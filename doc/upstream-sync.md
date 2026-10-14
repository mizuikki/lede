# 上游同步实施与部署

本方案已在仓库内实现，默认只允许人工启动；没有配置 `schedule`。本轮没有
推送候选、创建 PR、运行 GitHub workflow 或改仓库规则。固件构建与发布仍须在
部署阶段验证。

## 基线与最新核对

设计基线为 `8ea4ab1af9c40e5e68bfab5bc89f4c7cf71feb1e`。2026-10-14 对远端
只读复核得到下表；这些 SHA 是本次经验证的初始化状态，不是以后运行时写死的
同步目标。每次运行都重新读取 fork 和 upstream 的当前默认分支，再固定完整
提交 SHA。

| 项目 | 本轮复核值 |
| --- | --- |
| fork 默认分支 | `master`，`2abb33d9b48e615081b64b35ef2b4f78b68bd6f2` |
| upstream | `coolsnowwolf/lede:master`，`dcafc376a688efb0887b661b3a98a4c41a725984` |
| 唯一 merge-base | `241650a12c1121c76ba6e82567f3cbd81e368d3d` |
| `B...U` 提交数 | fork 侧 67，上游侧 42；fork 侧包含历史导入，不等于独立定制补丁数 |
| 有效树差异 `M→B` | 126 个文件；包含 package、target、脚本和构建配置 |
| 冲突 | `target/linux/qualcommax/image/ipq60xx.mk` 中 `Device/EmmcImage` |
| 远端规则 | 默认分支 `master`；当时没有 classic protection，rulesets 列表为空 |
| 合并方式 | merge、squash、rebase 当时都允许；部署前仍需设置为只允许 merge |
| Actions 设置 | 仓库默认 token 权限为 write；工作流已逐 job 限权 |

`.github/upstream-state.json` 初始化记录已确认的共同历史点 `M` 和已复核的
fork 基线 `B`。后续候选合入后，state 随候选配置提交更新；同步程序仍会检查
旧 upstream SHA 和旧 fork base 都是新历史的祖先，并要求恰好一个 merge-base。
遇到重写、倒退或多 merge-base 会停止并生成 `history_review_required` 报告。

最新 EmmcImage 冲突报告包含 merge-base、fork base 和 upstream 三侧 blob ID。
fork 当前模板只声明 `factory.bin`、padding 为 6144k，并保留 sysupgrade 规则；
upstream 在迁移 Qualcomm DTS 时新增 `recovery.bin` 声明。冲突解决必须保留上游
DTS 迁移和其他设备变更，再按设备实际镜像路径逐项审查模板。禁止整文件采用
ours/theirs。相关设备包括 AnySafe E1、ZN M2、JDCloud RE-CS-02/07、RE-SS-01、
Link NN6000 v1/v2、Philips LY1800、Redmi AX5 JDCloud。

## 已实现的行为

### 状态、候选和历史

- `scripts/upstream-sync.py` 每次固定 `B`、`U`、`W`，验证上游仓库/分支、历史
  单调性和唯一 merge-base。报告包含 `M→B`、`M→U`、`B→C`、`U→C` 文件数、
  加删行数和路径；冲突报告包含三侧 blob ID 与复现命令。
- 若 `U` 已是 `B` 的祖先，结果是 `no_updates`，不推分支、不建 PR、不启动构建。
- 无冲突时生成两提交候选：先是父链为 `B`,`U` 的 merge commit，再提交 state、
  feeds lock 和候选 manifest。人工冲突解决通过 `register` 验证相同父链后登记。
- 分支和 `sync_id` 使用 `B12-U12-W12`：
  `sync/upstream-<B12>-<U12>-w<W12>`。manifest 仍保存完整 SHA；缩写碰撞或
  旧候选身份不匹配时会停止，保留旧分支，不覆盖或强推。
- controller 先查找唯一开放的 `sync/upstream-*` PR，再读取 manifest 中固定的
  `B/U/W`。没有候选时，`candidate_branch` 只表示计划分支；只有 `ls-remote`
  确认且 fetch 成功后才输出非空 `candidate_ref` 并传给 prepare helper。`register`
  同样要求这个已 fetch 的候选引用。即使上游 `U` 已前进，仍可按旧候选身份报告、
  验证和重试，不会因为上游每次前进就另建候选 PR。若默认分支 `B` 前进，旧候选报告为
  `candidate_expired`；仅非 report、非 dry-run 的 prepare/register 才将旧 `C` 的
  `upstream/ready` 置为 failure。report 与 dry-run 只生成本地报告和 Actions artifact，
  不写 status、分支、tag、PR、workflow dispatch 或 release。过期候选的构建与人工
  证据失效。维护者关闭过期 PR 后，须再次显式运行 `prepare` 才会准备替代候选。
- `report` 不刷新 lock；`prepare` 对已有候选复用原 lock。新候选只在首次准备
  时生成 lock，人工 `register` 也只在首次登记时生成。重试不会追随 feed tip。
- 候选修改后会以新的实际 HEAD 重新验证。`B` 前进后旧候选身份不匹配，需要
  新候选；不能沿用旧状态。重复触发会复用同身份的活动 build，已有成功状态且
  artifact 未过期时也复用；已完成但无可复用结果时要求显式 `retry_build`。
  真正创建新 dispatch 前，controller 先把候选 `upstream/ready` 设为 pending，
  防止旧成功状态在重试或 artifact 过期期间继续放行。
- 目前没有 schedule，因此 controller 只会在非 dry-run 的人工
  `workflow_dispatch` 下推候选分支、建 draft PR 和 dispatch build；从不写
  `master` 或强推候选。

手动冲突解决先在隔离目录固定 `B/U`，以 `B` 为第一父提交创建 merge，再逐个
审查冲突 hunk：

```sh
B=YOUR_FULL_BASE_SHA
U=YOUR_FULL_UPSTREAM_SHA
W=YOUR_FULL_WORKFLOW_SHA
CANDIDATE_BRANCH="sync/upstream-${B:0:12}-${U:0:12}-w${W:0:12}"
git checkout --detach "$B"
git merge --no-commit --no-ff "$U"
# 手工审查并编辑冲突；不要整文件采用 ours/theirs
git add path/to/reviewed-file
git commit -m "Merge upstream candidate"
git push origin "HEAD:refs/heads/$CANDIDATE_BRANCH"
```

随后从默认分支人工启动 `Sync-Upstream`，选择 `register` 并传入该候选分支。
程序验证 merge 父链和文件状态，生成 lock/state/manifest 的登记提交；候选 PR
和构建仍由 controller 完成。

### fork contract 与功能边界

`.github/fork-contract.yml` 和 `scripts/verify-fork-contract.py` 检查：

- Passwall feed 顺序、LuCI 分支，以及安装后的 provider symlink；
- x86 profile 中每个 diffconfig 值是否在实际 `.config` 中保留；失去 target
  选择或把禁止的 `trojan-plus` 设为 y/m 都会失败；
- contract 自身固定必需的 target、Passwall/transproxy、Docker、firewall、代理和
  存储选择；候选 profile 与生成 `.config` 分别对照可信 contract，二者同时删掉
  选择也会失败。配置迁移须先审查并显式更新 contract；
- LAN 默认值 `192.168.5.1`、DHCP start/limit `110/145`；
- 官方 Perl overlay 和 dockerd patch 的锁定摘要；
- Airoha、Qualcomm、Rockchip 设备路径/关键字段、AN7583 `CONFIG_CRYPTO_HW`，
  以及 Qualcomm EmmcImage 与设备清单。
- 候选中的 workflow 仍保留 read-only/artifact-only build、显式 source check 和
  独立 default-branch publisher；controller 的 push 只能指向候选分支。候选若
  改动发布权限边界或 master 写入路径会失败。

contract 提供可自动核对的保护项，不替代对全部 126 个有效树差异的人工语义
审查。保留历史导入设备功能；上游已有缺陷继续按本仓库既定边界跟踪，不借
同步增加 driver workaround 或升级 kernel/backport patch。x86 CI 不能证明设备
镜像、升级、校准或实机网络行为正确。

### feeds lock 与构建输入

`.github/feed-lock.json` 记录所有启用 feed 的 URL、配置分支、顺序和完整 SHA，
并固定官方 Perl 的完整 revision、目录 tree 和内容摘要，以及 dockerd patch 的
SHA-256。候选的 manifest 保存 lock 摘要。`scripts/feed-lock.py` 可创建、物化、
校验和计算 lock 摘要。

`scripts/sync-feeds.sh --locked` 临时生成 `feeds.conf` 并按锁安装 feeds，再以
锁定 Perl revision 覆盖 packages feed、staging dockerd patch、安装 feed packages，
最后检查所有 feed HEAD、Perl 内容、补丁内容和 packages feed 非预期改动。它拒绝
覆盖现有 `feeds.conf`。`apply-profile.sh --locked-feeds` 先执行锁定安装再展开
profile；`make` 重试使用已安装的同一输入，不刷新 feed 或 lock。

构建在开始和结束时核对 lock 与 fork contract。artifact 保存 lock、实际 feed
revision、`.config`、diffconfig、package manifest、buildinfo、checksums、contract
结果和 `sync-validation.json`。另外单独上传仅含 validation JSON 与 lock 的小型
artifact，供状态汇总读取；汇总器不下载/解压固件大包。这样锁定源码输入，不承诺
工具链环境或全部下载源字节级可复现。汇总需要大构建 artifact 与小验证 artifact
都仍有效；任一过期都会阻止 ready。

### Actions 版本和异步结果

`Sync-Upstream` 只允许 `workflow_dispatch`。它将当前可信 controller SHA `W`
写入 `ci/sync-control-<W12>` 固定 tag，并显式 dispatch `openwrt-ci.yml`，
传递候选 `C`、`B/U/M`、lock 摘要和 `sync_id`。代码固定 REST API 版本
`2026-03-10`，要求 dispatch 响应包含 `workflow_run_id`，随后核对 workflow ID、
event、tag ref、head SHA、run title、source SHA 和 attempt。没有 ID、身份不符、
错误 attempt 或重复活动 run 不会退化为寻找“最近一次运行”。

候选 `pull_request` 预览构建被跳过，不会写 `upstream/ready`。普通 PR 也不直接由
PR 自带的 workflow 构建：`pr-build-dispatch.yml` 在 `pull_request_target` 的可信
base 版本中运行，只读取 PR 的精确 head/base/merge SHA，不 checkout 或执行 PR
代码；它校验当前 PR 和默认分支后，将只读构建 dispatch 到固定的
`ci/sync-control-<W12>` tag。构建使用 PR merge SHA，GITHUB_TOKEN 只有 contents
read。PR 即使修改或删除编译 workflow 中的步骤，也不能改动这次已固定的控制版本。

PR 汇总器另外读取 GitHub API 中 base 与 merge commit 对应的
`.github/workflows/openwrt-ci.yml` blob，要求完全相同；并要求 PR merge commit 的
父提交顺序严格为 `[base, head]`。artifact metadata 的 workflow SHA 必须等于 PR
base SHA，dispatch run 的 ref/head SHA 必须对应同一个 `W`，metadata 中的 PR
number/head/base/merge SHA 还须与当前 GitHub PR 完全一致。仅有 workflow ID、job
名称、artifact 自报值或 PR 的 `github.workflow_sha` 都不能建立信任。候选 PR 也
只接受显式 dispatch 的固定控制版本；候选的 `upstream/ready` 写在实际候选 HEAD。

`sync-result.yml` 在 `workflow_run: completed` 和 PR 评论变更后从默认分支运行可信
汇总程序，读取精确 run/attempt 的 jobs 和 artifact，核对候选当前 HEAD、PR
head/base/merge SHA、当前默认分支、候选 manifest、feed lock 和实际 feed revisions。
`validate_source`、`build_openwrt` 必须都为 success；skipped、失败、run/attempt
不匹配、artifact 缺失/过期、候选被改或默认分支前进都不能通过。汇总器只在当前
默认分支 workflow 中运行，写 status 的 token 不用于执行 PR 代码。

实际差异按固定 `B/U/C`（普通 PR 使用精确 base/merge）送入
`scripts/platform_impact.py`，生成平台必需检查计划。当前映射为：Qualcomm IPQ60xx
要求 `image_build`、`dts_review`、`upgrade_review`；Airoha AN758x 要求
`image_build`、`upgrade_review`、`device_review`；Rockchip RK3568 要求
`target_build`、`sgmii_qsgmii_review`。通用 kernel/target/toolchain 路径按全部平台
处理。`validate_source` 和 x86 构建成功不替代这些验证。每项证据都须通过 PR
评论中的 `upstream-sync-evidence` JSON marker 绑定到当前候选 SHA，并由非 PR
作者、针对当前 head SHA 的最新 APPROVED review 提交；证据包含结果、HTTPS URL
和 SHA-256。缺少证据保持 pending，失败证据使状态失败；候选 SHA 或 PR head 改变
后旧证据不能复用。评论被编辑或删除时会重新聚合。汇总器检查 URL 与摘要格式，
不下载证据文件；证据内容仍由 reviewer 审核。先对当前 HEAD 提交 APPROVED review，
再发布或编辑证据评论，以触发可信汇总器重新计算。每次评论重算会先把当前目标
status 设为 pending，因此 API/证据读取失败也不会留下旧 success。例如：

```text
<!-- upstream-sync-evidence:{"candidate_sha":"<full SHA>","platform":"qualcomm_ipq60xx","check":"image_build","result":"pass","evidence_url":"https://example.invalid/evidence","sha256":"<64 hex SHA-256>"} -->
```

候选 PR 查询使用 REST `GET /repos/{owner}/{repo}/pulls` 的 `head=owner:branch`
过滤器（不是 `owner/repo:branch`），并进行 URL 编码。汇总器要求唯一结果的
`head.ref` 等于完整 `sync/<sync_id>`、`head.repo.full_name` 等于当前仓库、`head.sha`
是完整 40 位 SHA 且等于 controller run-name 中的 source SHA；随后校验 PR base 与
manifest 固定的 `B`、manifest 中完整的 `B/U/W` 和精确 run-name。fork PR、错误或缩短
SHA、重复 PR、模糊标题都不能被当成候选。评论触发聚合使用同一 branch、repo、SHA、
manifest 和 run-name 规则；缺少平台证据时保持 pending。

成功/失败/pending 写在实际候选 commit 或普通 PR merge commit 的
`upstream/ready` status context。普通 PR 不要求修改 upstream state。
同一 run 的 attempt 汇总串行处理；若旧 attempt 的 webhook 晚于新 attempt 到达，
旧结果以 neutral 忽略，不写 status。若同一候选有更新的独立 run，或同一 PR 有
更新的构建 run，旧 run 也以 neutral 忽略，避免较晚到达的旧 webhook 覆盖新结果。

workflow dispatch REST API 在本轮采用的官方版本 `2026-03-10` 返回
`workflow_run_id`（HTTP 200 JSON）；代码不再实现旧版空 204 响应的“找最新 run”
兜底。这是根据 REST 文档确认的版本契约，不是未验证的行为假设。

### 构建和发布隔离

`.github/workflows/openwrt-ci.yml` 的 `validate_source` 与 `build_openwrt` 权限
为只读；checkout 关闭持久凭据，构建只上传 14 天 artifact。候选 artifact 永不
进入 publisher。

独立 `publish_release` 仅能从默认分支 `workflow_dispatch` 且没有 `sync_id` 时
运行。它下载本次 run 的 artifact，核对源码 SHA 等于 dispatch SHA 和发布前重新
读取的当前默认分支 HEAD，检查 workflow/source/run/attempt/profile/lock metadata
与 checksums 后才写 history/latest release。默认分支构建 SHA 前进后会拒绝发布。

## 本地验证

本地回归测试以临时 Git 仓库和模拟 GitHub CLI/API 覆盖无更新、正常
merge、EmmcImage 三侧冲突、上游历史改写、多 merge-base、候选修改、base 前进、
手工冲突登记与重复复用、feeds/overlay staged/unstaged/untracked 漂移、profile 与
生成配置同时丢失必需选择、PR workflow blob 被替换或删除、任意 W/伪造 metadata、
受影响平台证据缺失与候选 SHA 变化、错误 run/attempt、skipped job、artifact
过期、release notes 中的反引号/美元符号/换行，以及发布写权限隔离。

执行：

```sh
python3 -B -m unittest discover -s scripts/tests -v
```

测试不访问 GitHub、不推送、不运行固件构建，也不安装真实 feeds。上线前仍需手动
验证 GitHub 的 `pull_request_target`、`workflow_dispatch`、`workflow_run` 与
`issue_comment` payload、status 在 PR 页面显示方式、PR 审批策略和真实 artifact
下载/API 权限。

## 部署步骤

### 1. 部署实现并设置仓库权限

审查本实现后通过正常 PR/merge 将工作流和 helpers 放到 `master`。合入这次实现
属于部署，不要在本地执行远端 push 或 merge。

在 Settings → Actions → General：

1. 将全仓库 `GITHUB_TOKEN` 默认权限设为 **read repository contents**；工作流
   中明确需要写入的 job 会使用声明的最小权限。
2. 允许本方案实际使用的 actions：`actions/checkout@v7`、
   `actions/upload-artifact@v7`、`actions/download-artifact@v7`。不要为此开放
   workflow 创建新的 PAT 或长期凭据。
3. 确认维护者可运行 Actions。`Sync-Upstream` 需要写候选分支、创建/更新 draft
   PR、创建固定 `ci/sync-control-*` tag、调用 workflow dispatch 并写候选 pending
   status；`PR-Build-Dispatch` 需要读取 PR、创建固定 tag、dispatch 和写 PR pending
   status；`Sync-Result` 需要读取 actions/artifacts、PR/reviews/comments 并写
   commit status。`OpenWrt-CI` 的 build job 只需要 contents read；只有独立
   publisher 有 release/write 权限。不要让 PR 构建使用有写权限的 token。
4. 为 `ci/sync-control-*` 配置 tag ruleset，禁止更新和删除，允许 controller
   创建新 tag；限制管理员绕过。程序会拒绝复用指向别的 `W` 的现有 tag，
   ruleset 则防止 tag 创建后被重指向。

### 2. 手动 report 与候选试运行

1. 从 `master` 手动运行 `Sync-Upstream` 的 `report`，同时勾选 `dry_run`。
   检查报告中的当前 `B/U/M`、唯一 merge-base、四组有效差异、候选分支身份和
   冲突 blob ID。
2. 运行 `prepare` 并勾选 `dry_run`，检查候选树、fork contract 和新 lock。
   这会在 runner 本地生成对象与报告，不推分支、不建 PR、不 dispatch。
3. 本轮已知 EmmcImage 文本冲突，人工完成上一节的固定 `B/U` merge 和专项
   review 后，运行 `register`。检查登记报告和 draft PR 的 `B/U/C/W/L`。
4. 确认候选 PR HEAD 等于报告 `C`，workflow run 使用固定 `W` 的控制 tag，且
   metadata 的 run ID/attempt、source SHA、lock digest 与候选一致。候选直接
   `pull_request` 预览不会计入 ready；等待固定控制版本的显式 dispatch build。
   对平台计划中的每项检查，reviewer 先对当前 HEAD 提交 APPROVED review，再发布
   绑定 `C` 的证据评论，然后确认 `upstream/ready` 离开 pending。
5. 如果构建失败或 artifact 已过期，审查原因后重新运行 controller，并勾选
   `retry_build`。同一成功且 artifact 未过期的 build 会幂等复用。

### 3. 验证受影响平台并配置 master 规则

本轮**没有**运行远端固件构建。候选需要完整执行 `x86_64-passwall-docker`
构建；另按差异验证以下受影响范围：

- Qualcomm IPQ60xx：上述九类设备的 factory/sysupgrade 产物、DTS、caldata 和
  upgrade 路径；逐设备核验 EmmcImage 的 6144k padding 与镜像声明。具备设备时
  实测 recovery/刷写和 sysupgrade 路径。
- Airoha AN7581 Gemtek W1700K 与 Nokia XG-040G-MD：UBI image、bootloader、fan
  服务和 upgrade；AN7583 确认 `CONFIG_CRYPTO_HW=y`。需要设备时实测启动与升级。
- Rockchip RK3568 ROCK 3B：6.18 SGMII/QSGMII patch 应用和相关 target 编译；有
  设备时实测网口与启动。

完成普通 PR 的可信 dispatch、`upstream/ready` 页面与平台证据验证，以及候选 PR
的显式 dispatch 验证后，再
为 `master` 配置规则：必须经 PR、required status `upstream/ready`、要求分支
更新到最新 base、禁止直接 push 和 force push、限制管理员绕过。同步候选须有
人工 review，并在 HEAD 改动后重新审查。仓库只允许 merge commit；关闭 squash
和 rebase，以保留 upstream merge 父链。若维护者人数不足以满足 GitHub 的
review 数量规则，应先指定另一名 reviewer 或书面确定规则例外，再开放候选合入。

### 4. 合入与正式发布

候选的 `upstream/ready` 和平台审查都通过后，确认 PR base 仍是报告中的 `B`、
HEAD 仍是已验证 `C`，使用 **Create a merge commit** 合入。禁止 squash/rebase。
合入后核对 upstream `U` 仍为 `master` 祖先，并记录实际默认分支合入 SHA `R`。

从 `master` 手动运行正式 `OpenWrt-CI`，source SHA 显式指定为 `R`，不传
`sync_id`：

```sh
R=YOUR_FULL_COMMIT_SHA
gh workflow run openwrt-ci.yml --ref master \
  -f source_sha="$R" \
  -f build_reason=upstream-sync-release
```

只有实际默认分支仍指向 `R` 的成功构建才能发布；若分支前进，保留 artifact，
对当前默认分支 SHA 重新构建。

### 5. 之后再开启 schedule

完成至少一次手动候选、候选构建、required status、人工 merge 和正式发布闭环，
再单独审查一个仅增加定时触发的提交。例如在
`.github/workflows/sync-upstream.yml` 保留 `workflow_dispatch` 并增加：

```yaml
  schedule:
    - cron: "17 3 * * *"
```

该 UTC 时间只作为维护者示例。schedule 仍运行相同的候选 prepare/controller，
不会直接更新 master；最多复用一个身份完全匹配的候选。发布 branch rule、权限、
artifact 保留期和通知责任都确认后，才合入这个 schedule 提交。保持
`retry_build` 默认关闭，失败/过期候选需人工判断后显式重试。

## 技术依据

- GitHub Actions context、`workflow_dispatch` 和 `workflow_run`：
  [Contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)、
  [Events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)。
- GitHub `pull_request_target` 安全说明（不得 checkout/执行不可信 PR 内容）：
  https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target
- Actions REST workflow runs API（精确 run ID/attempt）与 workflow dispatch：
  https://docs.github.com/en/rest/actions/workflow-runs
  https://docs.github.com/en/rest/actions/workflows
- GitHub CLI `gh pr list --json` field list（候选发现读取 `headRepository` 以排除 fork PR）：
  https://cli.github.com/manual/gh_pr_list
- GitHub REST pull request list 的 `head` 参数格式为 `owner:branch`，并返回 `head.ref`、
  `head.sha` 和 `head.repo`：
  https://docs.github.com/en/rest/pulls/pulls#list-pull-requests
- Workflow dispatch REST API：
  [Create a workflow dispatch event](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。
  Context7 核对了 API 与 Actions 语义；Tavily 于 2026-10-14 复核官方 REST 文档，
  dispatch 响应版本 `2026-03-10` 含 `workflow_run_id`。
- REST API 变更说明：
  https://docs.github.com/en/rest/about-the-rest-api/breaking-changes
- GITHUB_TOKEN 事件触发限制：
  [Automatic token authentication](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication)。
- Required status checks 和分支保护：
  [Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)。
- Actions 版本：沿用本仓库现有 checkout v7 和 artifact v7，没有借本次同步升级 action。
