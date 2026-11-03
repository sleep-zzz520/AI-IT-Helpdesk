"""Score explicit claim annotations for final answers in a RAG release report."""

import argparse
import json
from pathlib import Path

from scripts.eval_rag_release import digest, load_fixture
from scripts.rag_eval_scoring import _CITATION


def expected_cases(report, suite):
    if report["suite_sha256"] != digest(suite):
        raise ValueError("report suite digest mismatch")
    cases = {
        case["id"]: case for case in suite["cases"] if case["split"] == report["split"]
    }
    if not cases:
        raise ValueError("report split has no expected cases")
    return cases


def index_rows(rows, expected):
    result = {}
    for row in rows:
        case_id = row["case_id"]
        if case_id in result or case_id not in expected:
            raise ValueError(f"unexpected or duplicate case: {case_id}")
        result[case_id] = row
    return result


def score_row(row, annotation, expected_action):
    answer = row["answer"]
    injected = row["injected"]
    claims = annotation["claims"]
    relevant = annotation["answer_relevant"]
    if (annotation["claims_complete"] is not True or type(relevant) is not bool
            or not isinstance(claims, list) or not isinstance(injected, list)):
        raise ValueError("claim coverage and relevancy verdict required")
    citation_ids = set(_CITATION.findall(answer))
    verdict_ids = set()
    claim_keys = set()
    faithful = correct = covered = supported = links = valid_links = 0
    if not claims and not annotation.get("no_claims_reason", "").strip():
        raise ValueError("explicit no-claims reason required")
    for claim in claims:
        excerpt = claim["text"]
        if (not excerpt.strip() or excerpt not in answer or excerpt in claim_keys
                or type(claim["faithful"]) is not bool or type(claim["correct"]) is not bool
                or not isinstance(claim["citations"], list)):
            raise ValueError("unique answer excerpt and explicit claim verdicts required")
        claim_keys.add(excerpt)
        if claim["faithful"] and not injected:
            raise ValueError("a claim cannot be faithful without injected evidence")
        faithful += claim["faithful"]
        correct += claim["correct"]
        seen = set()
        for link in claim["citations"]:
            citation_id = link["id"]
            if (citation_id not in citation_ids or citation_id in seen
                    or type(link["supports"]) is not bool):
                raise ValueError("invalid or repeated claim citation verdict")
            if link["supports"] and int(citation_id) > len(injected):
                raise ValueError("unresolved citation cannot support a claim")
            seen.add(citation_id)
            links += 1
            valid_links += link["supports"]
        verdict_ids.update(seen)
        covered += bool(seen)
        supported += any(link["supports"] for link in claim["citations"])
    if citation_ids != verdict_ids:
        raise ValueError("all final-answer citation IDs must be reviewed")
    count = len(claims)
    empty = None if expected_action != "answer" else 0.0
    return {
        "faithfulness": faithful / count if count else empty,
        "factual_correctness": correct / count if count else empty,
        "citation_coverage": covered / count if count else empty,
        "supported_claim_rate": supported / count if count else empty,
        "citation_support": valid_links / links if links else (0.0 if count else empty),
        "answer_relevancy": float(relevant),
    }


METRICS = ("faithfulness", "factual_correctness", "citation_coverage",
           "supported_claim_rate", "citation_support", "answer_relevancy")


def score_review(report, suite, review):
    expected = expected_cases(report, suite)
    if review["schema_version"] != 1 or review["report_sha256"] != digest(report):
        raise ValueError("review schema or report digest mismatch")
    provenance = review["provenance"]
    if (provenance["kind"] not in {"human", "assistant_review"}
            or not provenance["reviewer"].strip() or provenance["rubric_version"] != 1):
        raise ValueError("invalid review provenance")
    annotations = index_rows(review["rows"], expected)
    observed = index_rows((row for row in report["rows"] if row["mode"] == "e2e"), expected)
    errors = []
    scored = {}
    for case_id, case in expected.items():
        try:
            row = observed.get(case_id)
            if row is None or row["errors"] or not row.get("answer", "").strip():
                raise ValueError("missing or errored final answer")
            if case_id not in annotations:
                raise ValueError("missing review")
            scored[case_id] = score_row(row, annotations[case_id], case["expected"]["action"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append({"case_id": case_id, "error": str(exc)})
            scored[case_id] = dict.fromkeys(METRICS, 0.0)
    metrics = {}
    for metric in METRICS:
        values = [row[metric] for row in scored.values() if row[metric] is not None]
        metrics[metric] = {"value": sum(values) / len(values) if values else None,
                           "cases": len(values)}
    complete = not errors and report["status"] == "completed"
    human_verified = complete and provenance["kind"] == "human"
    return {"complete": complete, "human_verified": human_verified, "expected_cases": len(expected),
            "reviewed_cases": len(expected) - len(errors), "errors": errors,
            "provenance": provenance, "metrics": metrics, "rows": scored}


def calibrate_review(report, suite, candidate, human):
    """Compare verdicts over the same excerpts and claim-to-citation links."""
    if not score_review(report, suite, human)["human_verified"]:
        raise ValueError("calibration requires a complete human reference")
    if candidate["provenance"]["kind"] != "assistant_review" or not score_review(
        report, suite, candidate
    )["complete"]:
        raise ValueError("calibration requires a complete assistant review")
    observed = {row["case_id"]: row for row in candidate["rows"]}
    pairs = {key: [] for key in ("faithful", "correct", "citation_support", "answer_relevant")}
    for row in human["rows"]:
        other = observed[row["case_id"]]
        pairs["answer_relevant"].append((row["answer_relevant"], other["answer_relevant"]))
        reference_claims = {c["text"]: c for c in row["claims"]}
        claims = {c["text"]: c for c in other["claims"]}
        if reference_claims.keys() != claims.keys():
            raise ValueError(f"claim decomposition mismatch: {row['case_id']}")
        for text, claim in reference_claims.items():
            for metric in ("faithful", "correct"):
                pairs[metric].append((claim[metric], claims[text][metric]))
            reference_links = {link["id"]: link["supports"] for link in claim["citations"]}
            links = {link["id"]: link["supports"] for link in claims[text]["citations"]}
            if reference_links.keys() != links.keys():
                raise ValueError(f"claim citation alignment mismatch: {row['case_id']}")
            pairs["citation_support"].extend((v, links[k]) for k, v in reference_links.items())
    return {metric: {
        "total": len(values),
        "agreement": sum(expected == predicted for expected, predicted in values),
        "false_accepts": sum(not expected and predicted for expected, predicted in values),
        "false_rejects": sum(expected and not predicted for expected, predicted in values),
        "human_negative": sum(not expected for expected, _ in values),
        "human_positive": sum(expected for expected, _ in values),
    } for metric, values in pairs.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("suite", type=Path)
    parser.add_argument("review", type=Path, nargs="?")
    parser.add_argument("--template", action="store_true")
    parser.add_argument("--human-reference", type=Path)
    args = parser.parse_args()
    if args.template == (args.review is not None) or (args.template and args.human_reference):
        parser.error("provide a review file or --template")
    try:
        report = json.loads(args.report.read_text())
        suite = load_fixture(args.suite)
        if args.template:
            result = {
                "schema_version": 1, "report_sha256": digest(report),
                "provenance": {"kind": "human", "reviewer": "", "rubric_version": 1},
                "rows": [{"case_id": case_id, "answer_relevant": None,
                          "claims_complete": False, "claims": []}
                         for case_id in expected_cases(report, suite)],
            }
        else:
            review = json.loads(args.review.read_text())
            result = score_review(report, suite, review)
            if args.human_reference:
                human = json.loads(args.human_reference.read_text())
                result["calibration"] = calibrate_review(report, suite, review, human)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result = {"complete": False, "human_verified": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(not ((args.template and "rows" in result) or result.get("human_verified")))


if __name__ == "__main__":
    raise SystemExit(main())
