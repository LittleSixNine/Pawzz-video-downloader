# 源码、私人数据与历史版本

## 开发入口

从项目根目录的 [README](../README.md) 开始；电脑使用见 [desktop.md](desktop.md)，Docker / NAS 部署见 [deployment.md](deployment.md)。`src/` 和 `web/` 是当前实现，`tests/` 使用虚构资料进行本地验证，`skills/` 提供 Agent 入口。

平台文件在 `platforms/desktop/` 与 `platforms/docker/` 分开放置；源码根目录保留原有启动入口与兼容链接。Docker 模板按发行包根目录使用，从源码启动也应在源码根目录运行 Compose，不能单独在模板目录内运行。发布生成器会将容器模板写成发行包根目录的普通文件，避免目标系统依赖源码兼容链接。

准备好两种 Mac FFmpeg 后，运行 `python tools/build_release.py /发行产物目录 --version 2026.10.28`，产物分别写入 `desktop/` 和 `docker/`。包内 `CONTENTS.sha256` 校验各文件，包旁 `.zip.sha256` 校验整个 ZIP。脚本仅收录指定源码、网页、说明及平台资源；不读取运行配置、账号或录像。生成的二进制、源码归档和发行包不提交 Git。

可以在源码目录之外建立一个本地工作区，分别保存：

| 目录 | 内容 | 是否上传公开仓库 |
|---|---|---|
| `Pawzz-video/` | 当前代码、通用文档与虚构测试 | 是 |
| `Pawzz-video-pravite/notes/` | 私人开发日志与交接 | 否 |
| `Pawzz-video-pravite/personal-data/` | 账号、设备资料、样片与研究证据 | 否 |
| `Pawzz-video-pravite/runtime/` | 配置、密钥与运行数据 | 否 |
| `Pawzz-video-pravite/archive/` | 旧代码快照、原始记录与迁移校验 | 否 |
| `Pawzz-video-pravite/deliverables/` | 发布原件和历史候选包的本地留档 | 否 |

这些是本地整理约定，不是安装要求，也不是程序自动创建的目录。`pravite` 是沿用的本地目录名。只在源码子目录执行 Git 操作；不要把整个工作区提交或作为 Docker 构建上下文。

## 使用哪份说明

日常安装使用 [GitHub Releases](https://github.com/LittleSixNine/Pawzz-video-downloader/releases) 的完整发布包及配套指南；当前开发版使用本分支的 README 和 docs。发布包是固定快照，内容不会随着 main 更新。

历史交接中的“当前”“待安装”“未发布”、临时路径、端口和测试数量均属于记录日期。旧源码快照与候选安装包只用于追溯，不应替代当前入口；判断正在运行的版本仍需核对实际服务。

迁移本地资料时，先按用途归档，保留原始记录，再补充现行目录索引；逐文件核对内容校验值并检查运行配置的路径引用后，才能清理旧副本。私人校验清单、研究证据与交接不随通用说明上传。
