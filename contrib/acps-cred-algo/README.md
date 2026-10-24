# acps-cred-algo

> 多智能体协作中的**能力可信度评分**与**复合排序**算法 —— 零外部依赖，可直接嵌入任何智能体编排系统。

在多智能体互联场景里，「谁的描述最匹配」和「谁真的能把活干好」往往是两件事。
发现服务给的是**语义匹配序**，而语义相似度并不等于真实交付能力。
本库提供一套可比较、可解释、抗操纵的量化方案，把「能力可信」变成可以参与排序的数字。

本包从 [ACPs-Cred](https://github.com/AIP-PUB) 智能体互联参考实现中抽出，
**不含任何协议实现、网络代码或业务逻辑**，只保留纯算法层。

---

## 核心思路

### 1. 用 Wilson 置信下界，而不是原始成功率

原始成功率对样本量极其敏感：`1/1 = 100%` 会压过 `98/100 = 98%`。
Wilson 下界自动对小样本保守：

| 观测 | 原始成功率 | Wilson 下界（95%） |
|---|---|---|
| 1 成功 / 1 次 | 100.0% | **20.7%** |
| 9 成功 / 10 次 | 90.0% | **59.6%** |
| 98 成功 / 100 次 | 98.0% | **93.0%** |

这就是「一次好评不能登顶」的数学基础，而不是靠人为加阈值。

### 2. 五个因子相乘，任何一项不合格都会拉低总分

```
cred01 = confidence × diversity × freshness × base

  base       = 0.75 × wilsonLower + 0.25 × latencyScore
  confidence = w_total / (w_total + k)          ← 证据充分度，w_total 是衰减后的权重
  diversity  = min(1, 0.6 + 0.2 × max(1, 来源数))       ← 单一来源降权
  freshness  = 1.0 或 0.5                       ← ACS 版本晚于证据时降权
```

**关键设计：`confidence` 用的是「时间衰减后的证据权重之和」，不是凭证条数。**
这样批量补录很久以前的凭证，无法提升置信度。

### 3. 排序时保留探索配额

纯按可信度排序，冷启动智能体永远垫底 → 永远拿不到调用 → 永远没有证据，
形成死循环。`cred_rank()` 会在最终序列靠前位置为冷启动候选保留名额，
以极小的排序代价打破这个循环。

### 4. 返回全部中间量，而不是一个黑盒分数

每次评分都返回 `ScoreDetail`，含样本数、成功/失败/中性计数、原始成功率、
Wilson 下界、时延均值、置信度、多样性、新鲜度、回查确认数。
**评分结果可以被复核、被质疑、被重算。**

---

## 安装

```bash
pip install -e .
```

或者直接把 `src/acps_cred_algo/` 拷进你的项目 —— **零第三方依赖，只需 Python ≥ 3.9**。

---

## 快速开始

```python
from acps_cred_algo import Receipt, ReputationStore, cred_rank, explain_ranking

# 1) 建库、灌凭证
store = ReputationStore()
store.ingest(Receipt(
    leaderAic="1.2.156.3088.1.BUPT.XXX.LEADER.0AAA",
    partnerAic="1.2.156.3088.1.BUPT.XXX.PARTNER.0BBB",
    skillId="demo.translate",
    terminalState="completed",
    durationMs=120,
    finishedAt="2026-10-20T12:00:00+00:00",
))

# 2) 查单个 (AIC, 技能) 的可信度
detail = store.score("1.2.156.3088.1.BUPT.XXX.PARTNER.0BBB", "demo.translate")
print(detail.cred, detail.tier, detail.wilsonLower, detail.confidence)
# 有凭证时按公式评分，tier 为 cold-start；只有无凭证时使用名义分 20.0。

# 3) 对发现结果做复合重排
ranked = cred_rank(
    [
        {"aic": "...AGENT_A.0AA1", "skillId": "demo.translate", "ranking": 1},
        {"aic": "...AGENT_B.0BB2", "skillId": "demo.translate", "ranking": 2},
    ],
    store,
)
for line in explain_ranking(ranked):
    print(line)
# 输出包含语义序、可信度、综合分和预检标记。上面两个候选均未录入凭证。
```

完整可运行示例见 [`examples/quickstart.py`](examples/quickstart.py)。

---

## 抗操纵设计

每一条防护都对应一种具体的攻击方式：

| 攻击方式 | 防御机制 | 实现位置 |
|---|---|---|
| 一次好评登顶（1/1 = 100%） | Wilson 下界 + 置信度因子 + `min_samples` 冷启动判定 | `reputation.py` |
| 批量补录旧凭证刷分 | 置信度按**衰减后权重**计算，不看条数 | `reputation.py` |
| 自己给自己好评 | `ingest()` 直接拒绝 `leaderAic == partnerAic` | `reputation.py` |
| 同一团队批量刷好评 | 来源多样性因子 + 同源折扣 `self_provider_weight` | `reputation.py` |
| 重复提交同一凭证 | 凭证摘要去重（SHA-256） | `reputation.py` |
| 借旧版本信誉卖新能力 | ACS 版本隔离 + `stale_acs_factor` | `reputation.py` |
| 冷启动永久没曝光 | 探索配额 `explore_quota` | `ranker.py` |

> ⚠️ **关于 `provider` 的语义**：`score(provider=...)` 里的 `provider` 是**被评价方所属团队**，
> 而 `Receipt.evidence.issuerProvider` 是**凭证签发方团队**。
> 两者相同会被判为「同源自评」并打 0.3 折扣 —— 这是设计意图，不是 bug。
> 在真实系统里，签发方通常是发起编排的那一方。

---

## 参数

所有超参数集中在 `ReputationConfig` 与 `RankConfig`，可直接调：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `half_life_days` | 14.0 | 时间衰减半衰期（天） |
| `prior_weight` (k) | 5.0 | 需要多少等效证据才算「可信」 |
| `wilson_z` | 1.96 | 置信水平（1.96 = 95%） |
| `latency_budget_ms` | 800.0 | 达到该时延即拿满时延分 |
| `success_weight` | 0.75 | 成功率与时延的权重分配 |
| `self_provider_weight` | 0.3 | 同源自评凭证的折扣 |
| `min_samples` | 3 | 低于该样本数视为冷启动 |
| `stale_acs_factor` | 0.5 | 证据过期时的整体降权 |
| `cold_start_cred` | 20.0 | 冷启动期名义分 |
| `semantic_weight` (α) | 0.4 | 排序中语义序的权重 |
| `cred_weight` (β) | 0.6 | 排序中可信度的权重 |
| `explore_quota` | 1 | 冷启动探索名额 |
| `explore_slot` | 3 | 探索名额插入位置 |

参数取值的依据与敏感性分析见 [`docs/ALGORITHM.md`](docs/ALGORITHM.md)。

---

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：Wilson 数学性质、冷启动、自评拒绝、去重、时间衰减、来源多样性、
ACS 版本隔离、持久化往返、复合排序、探索配额、可解释性字段完整性。

```
Ran 35 tests in 0.015s
OK
```

---

## 目录结构

```
acps-cred-algo/
├── src/acps_cred_algo/
│   ├── models.py       # 凭证模型（Receipt / Evidence / 摘要工具）
│   ├── reputation.py   # 能力可信度评分
│   └── ranker.py       # CredRank 复合排序
├── tests/              # 35 项单元测试
├── examples/           # 可运行示例
├── docs/ALGORITHM.md   # 算法推导、参数敏感性、局限性
└── pyproject.toml
```

---

## 与协议层的关系

本库**刻意不依赖任何智能体互联协议**。凭证的采集方式、传输格式、存储介质
都由调用方决定，本库只负责「拿到凭证之后怎么算」。

如果你使用 AIP / ACPs 生态，凭证可以直接由 `TaskResult` 的
`taskId` + `statusHistory` 推导，无需新增任何协议字段：

```python
from acps_cred_algo import result_digest_of

digest = result_digest_of(task_result.taskId, [s.state for s in task_result.statusHistory])
```

这样对端可以在收到查询命令后复现同一摘要，实现「第三方回查」而不侵入协议。

---

## 贡献

欢迎提交 Issue 与 PR，尤其是：

- 更多抗操纵场景的构造与测试用例
- 不同业务场景下的参数调优经验
- 与其它信誉系统的对比实验

---

## 许可证

[木兰宽松许可证，第 2 版（Mulan PSL v2）](LICENSE) —— 与 ACPs 开源社区保持一致。
允许商用、修改、再发布，需保留版权声明与许可证文本。

---

## 引用

如果本算法对你的工作有帮助，可引用：

```
ACPs-Cred 参考实现工作组. acps-cred-algo: 多智能体能力可信度评分与复合排序算法[CP/OL].
(2026-10)[2026-10-24]. https://github.com/wushuaige0925-ship-it/ACPs-community/tree/add-acps-cred-algo/contrib/acps-cred-algo .
```
