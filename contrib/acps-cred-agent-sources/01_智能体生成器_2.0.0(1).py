from .common import ServiceUnavailable, llm

SECTIONS = ["目标用户", "任务范围", "拒绝范围", "输入输出", "依赖", "失败处理", "隐私安全边界", "验收测试"]
SYSTEM = """你是智能体方案生成器。优先面向梧桐 ACPs 02.02；输出方案、材料草案，不执行账号创建、注册、证书或部署。
JSON 字段：name、sections（对象，必须包含目标用户、任务范围、拒绝范围、输入输出、依赖、失败处理、隐私安全边界、验收测试八项）、
acs_draft（对象：protocolVersion=02.02、name、description、version、skills；不得编造 aic/provider 身份/证书）、
checklist（数组）、assumptions（数组）、unverified_requirements（数组）、citations（数组）。
需求足够则直接输出；必要信息不足可提出具体澄清项。所有平台事实要求引用输入 evidence 的实际引文或列入 unverified_requirements。
ACS 只是草案，不声称已注册、通过审核、已部署。方案含具体实现步骤与可执行测试。收到 review 时只修复所指出的问题。"""


def run(payload):
    text = str(payload.get("requirement") or payload.get("text") or "").strip()
    if not text:
        return {"status": "awaiting-input", "missing": ["requirement：希望智能体帮助谁完成什么任务"]}
    try:
        result = llm(SYSTEM, {"requirement": text, "evidence": payload.get("evidence", []),
                              "previous": payload.get("previous"), "review": payload.get("review")})
        if not isinstance(result.get("sections"), dict) or any(not result["sections"].get(s) for s in SECTIONS):
            raise ServiceUnavailable("大模型方案缺少必需边界或验收项，未通过结构检查。")
        result["status"] = "ok"
        result["material_state"] = "草案；须核验平台要求、提供者身份及部署地址后再注册"
        return result
    except ServiceUnavailable as e:
        return {"status": "dependency-unavailable", "reason": str(e), "requirement": text,
                "required_sections": SECTIONS, "unverified_requirements": ["尚未产生可用方案；配置模型后重试。"]}
