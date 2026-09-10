"""OCR 测试：合成截图 → 错误码提取 → 全流程验证。

注意：截图是【合成数据】（Pillow 画的模拟 VPN 报错窗口），
按 AGENTS.md 原则明确标注，不伪装成真实用户数据。
"""
import base64
import io

from PIL import Image, ImageDraw


def make_screenshot() -> str:
    """生成一张合成 VPN 报错截图，返回 base64。"""
    img = Image.new("RGB", (480, 200), (30, 34, 42))
    d = ImageDraw.Draw(img)
    # 模拟报错窗口
    d.rectangle([20, 30, 460, 180], fill=(22, 28, 38), outline=(255, 255, 255))
    d.text((40, 55), "VPN Client", fill=(230, 235, 245))
    d.text((40, 85), "Error 800 - Connection failed", fill=(255, 90, 90))
    d.text((40, 115), "Certificate is expired", fill=(230, 235, 245))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    # 完整 data URL（自带 MIME，与前端协议一致）
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


if __name__ == "__main__":
    shot = make_screenshot()

    # 1) 单测 OCR 工具
    from app.tools.ocr import extract_error_code
    code = extract_error_code(shot)
    print(f"[OCR 单测] 提取到错误码: {code or '(空)'}")

    # 2) 全流程：带截图发消息（文本没写错误码，靠 OCR 补）
    from app.agents.graph import graph
    out = graph.invoke({
        "messages": [{"role": "user", "content": "VPN连不上，设备是Windows 11", "image": shot}],
        "user_id": "zhangsan",
        "actor_id": "zhangsan",
        "tenant_id": 1,
        "execution_source": "web_agent",
        "operation_id": "script-ocr:zhangsan",
        "trace": [],
    })
    print(f"[全流程] error_code: {out.get('error_code') or '(空)'}")
    print(f"[全流程] 节点: {[t['node'] for t in out.get('trace', [])]}")
    print(f"[全流程] 最终回复: {out['messages'][-1]['content'][:40]}")
