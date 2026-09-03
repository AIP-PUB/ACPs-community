"""测试用 AIC 生成辅助。生产路径不得依赖本模块中的默认第6/7级。"""

from app.utils.aic import generate_aic as _generate_aic
from app.utils.aic import generate_ontology_aic as _generate_ontology_aic

TEST_ARSP_CODE = "1"
TEST_PROVIDER_CODE = "00001"


def generate_aic(
    protocol_version: str = "1",
    *,
    manager_code: str = TEST_ARSP_CODE,
    provider_code: str = TEST_PROVIDER_CODE,
) -> str:
    """生成测试用实体 AIC，显式带上第6/7级。"""
    return _generate_aic(
        protocol_version,
        manager_code=manager_code,
        provider_code=provider_code,
    )


def generate_ontology_aic(
    protocol_version: str = "1",
    *,
    manager_code: str = TEST_ARSP_CODE,
    provider_code: str = TEST_PROVIDER_CODE,
) -> str:
    """生成测试用本体 AIC，显式带上第6/7级。"""
    return _generate_ontology_aic(
        protocol_version,
        manager_code=manager_code,
        provider_code=provider_code,
    )
