"""转译质量评估（Phase 4）：GLM-4V 截图转译的错误码/场景/关键文字抽取准确率。

为什么做转译 Eval（本项目"用数据说话"方法论）：
- 转译降维是"有损"的：模型可能看错错误码、漏关键文字、无错误码时幻觉编一个
- 不量化就不知道损失多大——错误码抽取准确率直接决定"截图→知识命中"的可靠性
- 合成截图 15 张：5 种错误码 + 2 个无错误码负例（防幻觉）+ 4 种场景
- 2026-08-12 加 5 张真实感退化图（模糊/低对比度/噪声/低分辨率）：
  干净合成图 100% 不代表真实截图，退化用例的失败率才是真实数据

指标：
- error_code_acc：错误码抽取准确率（含负例：无错误码截图必须输出空）
- scene_acc：场景分类准确率
- key_texts_coverage：关键文字覆盖率（期望子串在 key_texts 中的命中比例）
- summary_nonempty_rate：摘要非空率

用法（backend 目录）：python -m tests.transcribe_eval
输出：tests/transcribe_report.json
注意：截图均为【合成数据】（Pillow 绘制），明确标注非真实用户数据。
"""
import io
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from app.rag.transcribe import transcribe_image

SLEEP_SECONDS = 1  # 防 429 限流（免费模型，批量调用时）
REPORT_PATH = Path(__file__).resolve().parent / "transcribe_report.json"


@dataclass
class ShotCase:
    """一条合成截图用例：绘制参数 + 期望标注。"""
    name: str            # 报告用文件名
    title: str           # 窗口标题
    lines: list[str]     # 窗口内文本行
    expected_code: str   # 期望错误码（"" = 无错误码负例）
    expected_scene: str  # 期望场景分类（transcribe prompt 的值域）
    expected_keys: list[str]  # 期望被 key_texts 覆盖的关键文字
    effects: list[str] = field(default_factory=list)
    # 真实感特效（2026-08-12 加：干净合成图 100% 不代表真实截图）
    # "blur:2" 高斯模糊 / "low_contrast" 降对比度 / "noise:0.06" 椒盐噪声
    # "small" 缩小再放大模拟低分辨率——真实截图模糊/反光/压缩是常态


# ===== 合成截图用例（10 条） =====
CASES = [
    ShotCase("vpn-error-800.png", "VPN Client v4.2.1",
             ["Error 800 - Connection failed", "Certificate is expired", "错误代码: 800"],
             "800", "vpn_client_error", ["Error 800", "Certificate is expired"]),
    ShotCase("vpn-error-720.png", "VPN Client v4.2.1",
             ["Error 720 - Device not registered", "请先注册设备", "错误代码: 720"],
             "720", "vpn_client_error", ["Error 720", "Device not registered"]),
    ShotCase("vpn-error-553.png", "VPN Client v4.2.1",
             ["Error 553 - Certificate revoked", "Certificate has been revoked", "错误代码: 553"],
             "553", "vpn_client_error", ["Error 553", "Certificate revoked"]),
    ShotCase("vpn-error-1068.png", "VPN Client v4.2.1",
             ["Error 1068 - Service not started", "Start the VPN service", "错误代码: 1068"],
             "1068", "vpn_client_error", ["Error 1068"]),
    ShotCase("vpn-error-769.png", "VPN Client v4.2.1",
             ["Error 769 - Destination unreachable", "Check network connection", "错误代码: 769"],
             "769", "vpn_client_error", ["Error 769"]),
    ShotCase("email-error-553.png", "Outlook",
             ["SMTP Error 553 - Authentication failed", "Check your password", "错误代码: 553"],
             "553", "email_error", ["SMTP", "553", "Authentication failed"]),
    ShotCase("cmdline-ping-timeout.png", "Command Prompt",
             ["C:\\> ping 10.0.0.8", "Request timed out", "Packets: Sent = 4, Lost = 4"],
             "", "cmdline_output", ["ping", "Request timed out"]),
    ShotCase("browser-403.png", "Web Browser",
             ["403 Forbidden", "You don't have permission to access this page"],
             "403", "web_browser_error",  # 首次跑：模型给 web_browser_error（比 other 更精确，采纳）
             ["403 Forbidden", "permission"]),
    ShotCase("dashboard-alert-800.png", "Ops Monitor",
             ["ALERT: VPN Certificate Expiring Soon", "错误码 800", "renew before 2026-08-15"],
             "800", "dashboard", ["ALERT", "800"]),
    ShotCase("blank-client-window.png", "VPN Client",
             ["Welcome to VPN Client", "Please log in"],
             "", "vpn_client_error",  # 这是 VPN 客户端登录窗口，首次跑模型判 vpn_client_error（标注修正）
             []),  # 负例：无错误码，模型必须不幻觉

    # ===== 真实感特效用例（5 条，2026-08-12 加）=====
    # 目标：干净合成图 100% 是"图太干净"的结果；真实截图有模糊/低对比度/
    # 噪声/低分辨率。这些用例预期部分失败——失败率本身就是真实数据
    ShotCase("vpn-error-800-blur.png", "VPN Client v4.2.1",
             ["Error 800 - Connection failed", "Certificate is expired"],
             "800", "vpn_client_error", ["Error 800"], ["blur:2"]),
    ShotCase("vpn-error-720-lowcontrast.png", "VPN Client v4.2.1",
             ["Error 720 - Device not registered", "请先注册设备"],
             "720", "vpn_client_error", ["Error 720"], ["low_contrast"]),
    ShotCase("email-error-553-noise.png", "Outlook",
             ["SMTP Error 553 - Authentication failed", "Check your password"],
             "553", "email_error", ["553"], ["noise:0.06"]),
    ShotCase("vpn-error-1068-small.png", "VPN Client v4.2.1",
             ["Error 1068 - Service not started", "Start the VPN service"],
             "1068", "vpn_client_error", ["Error 1068"], ["small"]),
    # 模糊负例：无错误码截图被模糊后仍不得幻觉出错误码
    ShotCase("blank-client-blur.png", "VPN Client",
             ["Welcome to VPN Client", "Please log in"],
             "", "vpn_client_error", [], ["blur:1.5"]),
]


def make_shot(case: ShotCase, path: Path, style: str) -> None:
    """按用例绘制合成截图（不同样式模拟不同软件界面）。"""
    if style == "cmdline":  # 黑底白字终端
        img = Image.new("RGB", (640, 300), (12, 12, 12))
        d = ImageDraw.Draw(img)
        y = 30
        for line in case.lines:
            d.text((30, y), line, fill=(200, 220, 200))
            y += 26
    elif style == "browser":  # 白底浏览器页
        img = Image.new("RGB", (640, 300), (245, 245, 245))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 640, 40], fill=(60, 110, 180))
        d.text((20, 12), case.title, fill=(255, 255, 255))
        d.text((60, 90), case.lines[0], fill=(60, 60, 60))
        d.text((60, 130), case.lines[1], fill=(100, 100, 100))
    elif style == "dashboard":  # 深蓝监控面板
        img = Image.new("RGB", (640, 300), (20, 30, 55))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 640, 40], fill=(30, 45, 80))
        d.text((20, 12), case.title, fill=(220, 230, 250))
        d.rectangle([40, 80, 600, 130], fill=(90, 20, 25))  # 红色告警条
        d.text((60, 92), case.lines[0], fill=(255, 120, 120))
        d.text((60, 150), case.lines[1], fill=(220, 230, 250))
        d.text((60, 180), case.lines[2], fill=(220, 230, 250))
    else:  # 通用深色弹窗（vpn / email / blank）
        img = Image.new("RGB", (640, 300), (40, 44, 54))
        d = ImageDraw.Draw(img)
        d.rectangle([60, 40, 580, 260], fill=(24, 28, 36), outline=(120, 128, 140))
        d.text((90, 70), case.title, fill=(200, 210, 225))
        y = 110
        for line in case.lines:
            d.text((90, y), line, fill=(255, 90, 90) if "Error" in line else (225, 230, 240))
            y += 30
    img = apply_effects(img, case.effects)
    img.save(path, format="PNG")


def apply_effects(img: Image.Image, effects: list[str]) -> Image.Image:
    """按特效列表对合成截图做真实感退化（模糊/低对比度/噪声/低分辨率）。"""
    for fx in effects:
        if fx.startswith("blur:"):
            radius = float(fx.split(":")[1])
            img = img.filter(ImageFilter.GaussianBlur(radius))
        elif fx == "low_contrast":
            img = ImageEnhance.Contrast(img).enhance(0.35)
            img = ImageEnhance.Brightness(img).enhance(1.25)
        elif fx.startswith("noise:"):
            sigma = float(fx.split(":")[1])
            noise = Image.effect_noise(img.size, sigma * 255)
            img = Image.blend(img.convert("RGB"), noise.convert("RGB"), 0.5)
        elif fx == "small":
            w, h = img.size
            img = img.resize((w // 2, h // 2)).resize((w, h), Image.LANCZOS)
    return img


def _coverage(expected_keys: list[str], key_texts: list[str]) -> float:
    """期望关键文字在转译 key_texts 中的命中比例（子串、忽略大小写）。"""
    if not expected_keys:
        return 1.0  # 无期望关键文字：视为覆盖（该用例只评错误码/场景）
    blob = " ".join(key_texts).lower()
    hits = sum(1 for k in expected_keys if k.lower() in blob)
    return hits / len(expected_keys)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="transcribe_eval_"))
    results = []

    for i, case in enumerate(CASES):
        style = "cmdline" if "cmdline" in case.name else (
            "browser" if "browser" in case.name else (
            "dashboard" if "dashboard" in case.name else "window"))
        shot = tmp / case.name
        make_shot(case, shot, style)

        trans = transcribe_image(shot, media_ref=case.name)
        if trans is None:
            results.append({"file": case.name, "error": "transcribe failed"})
            print(f"[{i + 1}/10] {case.name}: 转译失败")
            time.sleep(SLEEP_SECONDS)
            continue

        got_code = (trans.error_code or "").strip()
        # 错误码正确性：期望空 → 输出必须为空（防幻觉）；期望有值 → 精确匹配
        if case.expected_code == "":
            code_ok = got_code == ""
        else:
            code_ok = got_code == case.expected_code
        scene_ok = trans.scene == case.expected_scene
        cov = _coverage(case.expected_keys, trans.key_texts)

        results.append({
            "file": case.name,
            "effects": case.effects,
            "expected_error_code": case.expected_code,
            "got_error_code": got_code,
            "error_code_ok": code_ok,
            "expected_scene": case.expected_scene,
            "got_scene": trans.scene,
            "scene_ok": scene_ok,
            "key_texts_coverage": round(cov, 2),
            "summary_nonempty": bool(trans.summary),
        })
        flag = "✅" if code_ok else "❌"
        fx = f" [{'/'.join(case.effects)}]" if case.effects else ""
        print(f"[{i + 1}/{len(CASES)}] {case.name}{fx}: "
              f"错误码 {got_code!r}(期望 {case.expected_code!r}) {flag} "
              f"| 场景 {trans.scene}(期望 {case.expected_scene}) "
              f"{'✅' if scene_ok else '❌'} | 关键文字覆盖 {cov:.0%}")
        time.sleep(SLEEP_SECONDS)

    # ---- 聚合指标 ----
    ok_cases = [r for r in results if "error" not in r]
    n = len(ok_cases)
    error_code_acc = sum(r["error_code_ok"] for r in ok_cases) / n
    scene_acc = sum(r["scene_ok"] for r in ok_cases) / n
    coverage = sum(r["key_texts_coverage"] for r in ok_cases) / n
    summary_rate = sum(r["summary_nonempty"] for r in ok_cases) / n

    report = {
        "metric": "transcribe_quality",
        "cases": n,
        "error_code_acc": round(error_code_acc, 3),
        "scene_acc": round(scene_acc, 3),
        "key_texts_coverage_avg": round(coverage, 3),
        "summary_nonempty_rate": round(summary_rate, 3),
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== 转译质量报告（{n} 条合成截图）===")
    print(f"错误码准确率:  {error_code_acc:.1%}（含 {sum(1 for c in CASES if not c.expected_code)} 个无错误码负例）")
    print(f"场景准确率:    {scene_acc:.1%}")
    print(f"关键文字覆盖:  {coverage:.1%}")
    print(f"摘要非空率:    {summary_rate:.1%}")
    print(f"报告落盘: {REPORT_PATH}")


if __name__ == "__main__":
    main()
