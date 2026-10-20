# 2026-10-20 上游同步候选

本轮固定以下输入，不追随运行期间继续前进的远端分支：

| 输入 | 完整提交 SHA |
| --- | --- |
| fork base / workflow | `2ed84633b437962121b6d03ce496d0e4b8c32321` |
| upstream master | `07cebbcf28f61637047645c3fedc829ee9cc3688` |
| 唯一 merge-base | `241650a12c1121c76ba6e82567f3cbd81e368d3d` |

上游包含 50 个尚未合入的提交，较共同祖先改动 797 个文件。fork 侧历史包含
74 个提交，不等于独立定制补丁数量。两侧唯一共同改动路径和文本冲突均为
`target/linux/qualcommax/image/ipq60xx.mk`。

## 合并结果

保留上游 Qualcomm DTS 迁移、`DTS_DIR` 设置及 Redmi AX5 JDCloud 的
`SUPPORTED_DEVICES` 兼容标识。`Device/EmmcImage` 保持仅声明 `factory.bin`、
factory padding 为 `6144k`，并保留 sysupgrade 规则。最新上游已采用相同
factory padding，但仍声明没有对应生成规则的 `recovery.bin`；本轮不恢复该声明。
Philips LY1800 的 fork 排序保留，其余上游变更原样合入。

候选保留父链为 fork base、upstream 的双亲 merge commit，随后登记 state、
feeds lock 和 manifest。定制功能继续由可信 base 中的 fork contract 验证。

## 同步控制器的补充修复

原控制器对全部差异执行 `git diff --check`，会把上游统一补丁中的上下文前缀
空格、缩进及空上下文行当成源码空白错误。本轮保持上游 `.patch` 字节不变，
将此检查限于普通源码文件。补丁能否应用和编译仍须通过目标构建验证。

本轮候选使用原可信 workflow SHA，不能使用候选自身修改后的控制器。
因此首次登记在本地运行原版 `upstream-sync.py register`，检查普通源码空白和
fork contract 后推送候选；构建仍通过原版 `sync-dispatch.py` 固定控制 tag、
源码 SHA、lock 摘要及精确 run/attempt。后续同步在本修复合入后可恢复控制器登记。

## 验收范围

- 完整构建 `x86_64-passwall-docker`，核验真实 Kconfig、Passwall/Docker/provider、
  feeds/overlay、禁用 SSR Plus+ 的 manifest/IPK 检查和构建 metadata。
- 核对 fw4/nftables、dnsmasq 2.93、DNS 集合更新、fullcone 与内核升级对代理和
  网络转发的影响。x86 编译成功不能代替这些运行时检查。
- Qualcomm IPQ60xx：相关设备镜像、DTS、升级布局，特别是 EmmcImage 用户。
- Airoha AN758x：镜像、升级、设备审查和 AN7583 `CONFIG_CRYPTO_HW`。
- Rockchip RK3568：目标编译及 SGMII/QSGMII 审查。

平台证据必须绑定当前候选完整 SHA，并按
[既有同步流程](upstream-sync.md)提交。本文不充当实机或 reviewer 证据。
候选通过自动检查和平台审查后才能以 merge commit 合入；之后针对实际默认分支
合入 SHA 重新正式构建发布。本轮继续保持手动触发。
