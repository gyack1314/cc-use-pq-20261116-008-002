# 科技人才线索采集枢纽

本地离线运行的科技人才供需线索采集器。接收来源登记文件与镜像批次，支持 JSON、CSV 和简单 HTML 表格，将岗位、培养计划和校企合作项目统一为可追溯记录。SQLite 保存批次、页、原始记录、版本、事件，以及独立的机构注册表、来源机构标识声明与人工机构确认决议；当前视图只交付有效且无争议的最新线索，并按生效人工确认划分机构边界。

## 快速运行

需要 Python 3.11 或更新版本。项目只使用标准库。

    python -m unittest discover -s tests -v
    python -m compileall -q talent_harvest
    python -m talent_harvest demo --workspace fixtures

演示命令使用临时数据库，不改动夹具。实际使用时先登记来源，再采集镜像：

    python -m talent_harvest --db harvest.sqlite3 register fixtures/source.json
    python -m talent_harvest --db harvest.sqlite3 ingest fixtures/manifest.json
    python -m talent_harvest --db harvest.sqlite3 current
    python -m talent_harvest --db harvest.sqlite3 audit

ingest 对失败页返回非零退出码，并将原因保存在数据库。修复本地镜像文件后，重新运行同一批次即可只重试未完成页；ingest --page N 可指定页。已登记批次的清单不可修改；需要新声明时使用新批次标识。撤回来源使用 withdraw SOURCE --reason TEXT，这会让来源记录退出当前视图，保留历史与审计事件。

## 输入契约

来源 JSON 包含 key、name、format（json、csv 或 html）、kind（position、program 或 partnership）、可选 field_map 与 trust（0–100）。field_map 的键是规范字段名，值是镜像中的字段名。

批次清单 JSON 包含 source、batch、带时区的 captured_at，以及顺序从 1 开始的 pages。每页包含相对 path 和对文件原始字节计算的 SHA-256。文件须位于清单目录下，UTF-8 编码，每页最多 2 MB，最多 10,000 条记录。JSON 页可用记录数组或 records 对象字段；CSV 的第一行为表头；HTML 页使用一个简单表格的首行作为表头。

每条记录需要 record_id、organization、title、region、discipline、quantity、published_at（YYYY-MM-DD）；reference 与 description 可选。organization_id 可选，用于承载镜像自报的机构标识（如统一社会信用代码，18 位大写字母数字，也接受 4–40 位的其他登记号）。数量为非负整数，零数量、未来发布日期、低可信来源和缺少明确引用的合作项目会被隔离。基础实体键由类别、机构、标题和地区确定；相同实体的不同来源若数量、学科或发布日期不一致，会列入争议而非当前可用数据。实体键是精确规范化后的匹配，不做模糊别名判断。

## 机构标识与人工确认

镜像中的机构标识与机构实体**分开保存**：`organization_id` 只是来源的一条声明，进入 `organization_identifiers` 表，绝不参与实体键计算，采集时也不会用未经确认的别名或标识改写原始机构名。同一标识被多个未确认机构持有时，记录为待决分歧（`org-disputes`），可正常采入但不自动合并。

机构边界只能由人工确认改变。`confirm-org DECLARATION.json` 登记一份决议（`left_org`、`right_org`、`basis`、`reason`），声明两个机构属于同一主体；决议带版本、状态与审计事件，重复提交幂等。确认后当前视图按机构组重新划分实体边界，同组机构的原始证据各自保留；同一岗位若出现相反数量，进入争议（`quantity_mismatch`）而不相加。`revoke-org RESOLUTION_ID --reason TEXT` 撤销确认，两个实体及其原证据立即恢复；之后对同一机构对再次确认会生成递增的新修订版本。

若后续镜像给出与生效人工确认冲突的机构标识——同一标识被声明给处于不同决议组的机构，或同一决议组内两个机构各自持有对方没有的信用代码（双向独有码）——相关页在提交前被识别并整页隔离（错误码 `organization_identifier_conflict`），不写入任何观察记录、不沿用旧决议；仅一方正常补充登记号、另一方的标识仍被完全覆盖时不判冲突。修复或撤销决议后重试即可解除。`preview` 会在不改动状态的情况下提示同样的冲突。来源撤回（`withdraw`）使其声明退出当前视图与冲突判定，历史观察、标识声明与审计事件全部保留，待决分歧中会标注撤回来源。

## 查询与运维

- current --quarantine 返回有效实体、争议与隔离记录，并附带 `organization_resolutions`（生效人工确认）与 `org_identifier_disputes`（待决标识分歧）；默认不附带隔离明细。
- organizations 列出机构注册表与决议分组；org-disputes 列出待人工确认的共享标识；confirm-org / revoke-org 管理人工确认生命周期。
- history、trace IDENTITY、record SOURCE ID、timeline SOURCE、batch-evidence SOURCE BATCH 追踪来源与修订。
- status SOURCE BATCH、operations、recovery、quality、audit 检查页级恢复和数据状态。
- summary、distribution、coverage、age YYYY-MM-DD、trend 和 conflicts 提供当前与历史摘要。
- snapshot FILE 写出当前视图与摘要；verify FILE SHA256 校验交付字节。
- preview SOURCE_JSON MANIFEST 在不登记批次的情况下检查本地文件；replay-check MANIFEST 对已完成批次复核原始镜像；checkpoint 读取游标快照。

CLI 将结果作为单行 JSON 写入标准输出，将合同错误写入标准错误。仓库中的机构、岗位和数量均为本地合成演示数据。
