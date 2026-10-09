# Docker / 群晖部署与更新指南

[返回 README](../README.md)。本指南面向 Linux Docker 和群晖 Container Manager；电脑手动导出见 [电脑版教程](desktop.md)。Docker 更新包是可在目标机器构建的源码包，不是预构建镜像。

## 开始前

准备 Docker Engine、Compose，以及一台自己拥有或获授权的趴趴爪喂食器 Pro。NAS / Linux 主机与喂食器需要在可互通的可信局域网中，主机还需访问厂商登录服务和 MQTT TLS 服务。其他型号、固件和网络环境需自行验收。

程序通过设备的固件维护通道读取原片，使用固定模板和路径校验，不录屏、不转码，不改设备录像或固件。首次安装默认管理密码为 `admin`；已有安装保留原密码。请通过 HTTPS 连接后在设置中修改。管理密码、会话、密钥、证书私钥和录像均不应放在源码目录、镜像构建上下文或公开日志中。

## 一、Docker 通用设置

### 1. 分开程序与数据

下载 Docker 更新包并解压到程序目录，例如 `/opt/pawzz-video`。四个数据目录放在程序目录之外；以下 Linux 路径仅为示例：

```text
/opt/pawzz-video/         程序、Dockerfile、compose.yaml
/srv/pawzz-data/
  config/                设置、管理密码、历史、加密登录文件
  keys/                  加密登录状态的独立解密密钥
  certificates/          HTTPS 证书和私钥
  videos/                原片、日期目录、校验记录及可选修复副本
```

这四个数据目录可以放在同一个已有共享文件夹内，不要求分别创建共享文件夹。密钥必须位于独立于 `config` 的目录，不能与登录文件一起公开分享或放进同一份不受保护的备份。数据目录可以自定义，但不要放在代码目录内。

### 2. 指定运行身份与权限

选择非 root 的专用主机账号，查询其数字 UID/GID：

```sh
id pawzz
```

`pawzz` 是示例账号名，请换成实际账号。容器不会继承创建它的管理员身份；Compose 的 `user` 决定程序运行身份。账号无需加入管理员组或获得 Docker socket 访问权限。

创建四个数据目录，让配置、密钥目录由该账号拥有；配置和密钥目录使用 `700`，私有文件使用 `600`。账号还需进入并读取证书目录、读取证书私钥，以及写入录像目录。Linux 新安装可由管理员针对上述四个目录设置；以下 `1000:1000` 必须替换为实际 UID/GID：

```sh
sudo mkdir -p /srv/pawzz-data/config /srv/pawzz-data/keys /srv/pawzz-data/certificates /srv/pawzz-data/videos
sudo chown 1000:1000 /srv/pawzz-data/config /srv/pawzz-data/keys /srv/pawzz-data/certificates /srv/pawzz-data/videos
sudo chmod 700 /srv/pawzz-data/config /srv/pawzz-data/keys /srv/pawzz-data/certificates /srv/pawzz-data/videos
```

复制证书后，也要让专用账号能读取文件；私钥保持 `600`。这些命令只针对新安装的示例目录；已有安装先备份并检查所有者，不对整个数据卷递归改权限。有 ACL 的系统还需检查实际生效权限。不要用 `chmod 777` 或特权模式代替身份匹配。

### 3. 填写 .env

在程序目录执行：

```sh
cp .env.example .env
```

编辑 `.env`。以下数字和路径都是示例，必须按本机身份和目录替换：

```dotenv
PAWZZ_UID=1000
PAWZZ_GID=1000
PAWZZ_CONFIG_DIR=/srv/pawzz-data/config
PAWZZ_KEY_DIR=/srv/pawzz-data/keys
PAWZZ_SECRETS_DIR=/srv/pawzz-data/certificates
PAWZZ_OUTPUT_DIR=/srv/pawzz-data/videos
PAWZZ_WEB_PORT=18080
PAWZZ_TLS_CERT=/run/secrets/fullchain.pem
PAWZZ_TLS_KEY=/run/secrets/privkey.pem
```

`PAWZZ_UID`、`PAWZZ_GID` 为必填。前四个目录变量是主机路径；证书变量是容器内路径。不要把主机路径与容器路径混用。

| 主机目录变量 | 容器内路径 | 挂载模式 |
|---|---|---|
| `PAWZZ_CONFIG_DIR` | `/config` | 读写 |
| `PAWZZ_KEY_DIR` | `/keys` | 读写 |
| `PAWZZ_SECRETS_DIR` | `/run/secrets` | 只读 |
| `PAWZZ_OUTPUT_DIR` | `/videos` | 读写 |

冒号左侧是主机目录，右侧是容器目录。例如 `/srv/pawzz-data/videos:/videos`；无需在主机另外创建 `/videos` 或 `/run/secrets`。

### 4. 准备 HTTPS

将可信证书的完整链 `fullchain.pem` 和对应私钥 `privkey.pem` 放入证书目录。如果证书导出为叶证书 `cert.pem` 与中间链 `chain.pem`，依次合并为完整链；不要把私钥拼入完整链。示例：

```sh
cat cert.pem chain.pem > fullchain.pem
```

使用证书覆盖的域名访问管理页面，并让该域名在访问端解析到主机。浏览器必须正常验证证书；不要忽略证书警告。启用 TLS 后，同一端口只接受 HTTPS。

NAS 短信登录和修改管理密码要求本程序实际提供 HTTPS。只在反向代理终止 HTTPS、再以 HTTP 转给容器，不能满足该要求；程序不信任 `X-Forwarded-Proto`。反向代理如需使用，应转发至容器的 HTTPS 并验证上游证书。证书续期后需更新实际挂载文件，并在无任务运行时重启加载；手工复制的证书不会自动同步。

### 5. 构建并启动

在程序目录执行：

```sh
docker compose config --quiet
docker compose up -d --build
```

首次构建需要下载基础镜像和依赖。打开 `https://证书覆盖的域名:18080`，端口按实际设置替换。首次输入 `admin`，在“设备与存储 → 修改管理密码”中设置自己的密码。密码不能为空，不限制复杂度或最小长度；HTTP 请求体仍有 8 KiB 上限。修改立即生效，不用重启，其他已连接页面需使用新密码重新连接。

本模板使用 Linux host 网络、只读容器文件系统、`cap_drop: ALL` 和 `no-new-privileges`；数据挂载仍按表格读写。不要改成特权容器或挂载 Docker socket。Mac Docker 的虚拟网络不同，不是本指南的设备联调目标；Mac 导出请用电脑版入口。

## 二、群晖 Container Manager 设置

### 1. 准备账号和目录

在 DSM“控制面板 → 用户与群组”建立或选择专用普通账号，例如 `pawzz`，不加入 `administrators`。给它数据目录所需权限，其他私人共享文件夹按需要设为禁止访问；检查所属组和最终权限预览。

源码可放在 `docker` 共享文件夹的程序子目录，例如 `/volume1/docker/pawzz-video`。数据放在已有的数据共享文件夹中；以下 `app-data` 只是示例名称：

```text
/volume1/docker/pawzz-video/           解压后的程序
/volume1/app-data/pawzz/
  config/
  keys/
  certificates/
  videos/
```

在 File Station 中为配置、密钥目录设置正确所有者和权限；证书私钥仅供专用账号和管理员读取。程序源码由管理员管理即可，运行账号只需要访问挂载的数据。

查询 UID/GID 时，在 DSM“控制面板 → 终端机和 SNMP”开启 SSH，从电脑终端用管理员账号连接 NAS，再运行 `id pawzz`。不要为了查询而给专用账号增加管理员权限；密码只在自己的终端输入。容器终端通常不含 NAS 账号列表，不能用它查询主机用户名。

### 2. 配置项目

将 Docker 更新包解压到程序目录，把 `.env.example` 复制为 `.env`。按上一节填写实际 UID/GID，将四个目录变量换成 NAS 的绝对路径。证书容器路径仍为 `/run/secrets/fullchain.pem`、`/run/secrets/privkey.pem`。

在“Container Manager → 项目”创建项目，选择包含 `Dockerfile`、`compose.yaml`、`.env` 的程序目录，使用现有 Compose 文件构建并启动。YAML 配置页也可编辑并部署新设置，菜单名称可能随 DSM 版本变化。[群晖项目管理说明](https://kb.synology.cn/zh-cn/DSM/help/ContainerManager/docker_project)

如果使用界面直接填写 YAML 而没有读取 `.env`，将模板里的 `${PAWZZ_UID:?...}:${PAWZZ_GID:?...}` 替换为实际数字 `UID:GID`，四条挂载的主机侧替换为实际绝对路径；网页端口和 TLS 环境变量也填为实际值。保持 `user`、`environment`、`volumes` 的缩进层级，不新建重复的 `volumes` 段。数字 UID/GID 是运行身份，不在容器 `user` 中填 NAS 用户名。

直接填写 YAML 时，可以使用以下完整示例。`1234:100`、四个主机目录都是示例，部署前替换；在解压后的程序目录构建：

```yaml
services:
  pawzz-exporter:
    build: .
    image: pawzz-video:local
    user: "1234:100"
    network_mode: host
    restart: unless-stopped
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    stop_grace_period: 60s
    environment:
      TZ: Asia/Shanghai
      PAWZZ_MODE: server
      PAWZZ_CONFIG_DIR: /config
      PAWZZ_WEB_PORT: 18080
      PAWZZ_TLS_CERT: /run/secrets/fullchain.pem
      PAWZZ_TLS_KEY: /run/secrets/privkey.pem
      PAWZZ_SESSION_KEY_FILE: /keys/session.key
      PAWZZ_SESSION_FILE: /run/secrets/session.json
      PAWZZ_OUTPUT_DIR: /videos
    volumes:
      - /volume1/app-data/pawzz/config:/config
      - /volume1/app-data/pawzz/keys:/keys
      - /volume1/app-data/pawzz/certificates:/run/secrets:ro
      - /volume1/app-data/pawzz/videos:/videos
```

### 3. 导出或准备证书

可在 DSM“控制面板 → 安全性 → 证书”管理并导出已有证书，将完整链和私钥复制到数据证书目录。选择与实际访问域名匹配的证书，并确认链完整、专用账号可读。[群晖证书说明](https://kb.synology.com/en-us/DSM/help/DSM/AdminCenter/connection_certificate)

域名解析与路由应使浏览器能访问 NAS 的网页端口；网页访问路径与设备回调地址分别设置，不把公网域名填作设备局域网 IP 或回调 IP。

### 4. 检查实际生效设置

在容器详情核对运行用户、卷挂载及环境变量。打开 HTTPS 页面，诊断页应显示所选数字 UID/GID，以及配置、密钥和输出目录状态。在“存储位置”点击“检查实际写入权限”，确认临时文件写入、读取、删除通过；它不修改已有录像。

需要容器终端时选择 `/bin/sh`；Alpine 镜像不包含 `/bin/bash`。不要把凭据、私钥或含敏感环境变量的完整检查结果贴进公开日志。

## 三、登录、设备设置与首次导出

1. 在“设备与存储”使用官方 App 的手机号和短信验证码登录。默认仅临时保留；需要跨容器重启归档时勾选加密保存。
2. 设备局域网 IP 是喂食器的 IP；回调 IP 是保存录像的 NAS / Linux 主机的局域网 IP，不是容器虚拟地址、网页域名或 `127.0.0.1`。
3. 填写设备序列号和回调端口（默认 `10003`）。序列号用于 MQTT 设备主题寻址，目前必填，不能用手机号代替。网页端口与回调端口不能相同。
4. 明确允许维护读取，保存设置，保持自动归档关闭。首次可指定一个已完成原片验收；不知道源路径则留空。
5. 点击“运行一次”，核对日期目录下的 MP4、`.mp4.json` 校验记录、字节数和任务结果。输出路径是所挂载主机目录，程序保留日期目录和原文件名。

两个 IP 必须是 RFC1918 私网 IPv4。NAS 防火墙需允许喂食器访问回调 TCP 端口，并允许主机连接厂商服务；不要把回调端口暴露到公网。不要让多个导出实例同时控制同一设备或共用同一 MQTT 账号。

## 四、登录保存与运行计划

加密保存只写入 `user_id`、`token` 的密文，不保存手机号或验证码。密文在 `/config/login/session.enc`，独立随机密钥在 `/keys/session.key`；文件以 `600`、专用目录以 `700` 创建。已有密钥和密文不会被覆盖。

保存失败时，页面明确显示本次为临时登录和具体失败步骤；不会悄悄写明文。修复目录权限后点击“加密保存当前登录”，不用重新短信验证。临时登录在任务完成或普通失败后保留，在账号退出或容器关闭时清除。仅已加密保存的登录可在重启后尝试恢复，厂商会话过期仍需手动重新登录。

手动导出通过后，可开启自动归档：保存后立即执行一轮，之后按每轮结束后的间隔运行。可选 1、3、6、12 小时或 1、2、3 天，默认 1 天。整轮失败每 10 分钟重试，最多追加 3 次；仍失败则等待下一常规周期。单段读取超时在本轮额外重试最多 3 次，间隔 5 秒，期间复用设备通道；本轮完成、失败或主动停止后尝试关闭通道，不退出账号。

空间不足 5 GiB 时暂停，不自动删除旧录像；自动模式每 10 分钟检查空间，手动模式需要重新点击运行。约 10 分钟内或仍在写入的录像留到下次处理。

可选无损缩略图修复默认关闭，Docker 镜像内含 FFmpeg。开启后保留原片，在 `repaired/日期/` 保存独立修复副本，不重新编码，核对流 SHA-256；需要额外磁盘空间。

## 五、更新与回退

1. 等待任务结束，停止原服务。备份项目 YAML、私有 `.env`、配置、原密钥及证书，并保留旧程序或旧镜像；备份按凭据保护，不上传到公开仓库。
2. 将新版程序解压到独立临时目录。保留实际部署的 `compose.yaml`、`.env`，以及现有运行用户、挂载、网络、端口和 TLS 设置，不用通用模板覆盖定制配置。旧版未设置 UID/GID 的安装需先补充专用账号配置。
3. 用新版 `src/`、`web/`、`Dockerfile`、`requirements.txt` 更新原程序，保留 LICENSE；不要改动数据目录。
4. Linux 执行 `docker compose up -d --build`；群晖在原项目中重新构建并部署容器。只有停止再启动旧容器，不会更新镜像中的程序。
5. 检查运行身份、挂载、管理密码、登录恢复、设备设置和目录写入，再手动验收一轮。自动计划已启用的安装，重启后可能立即运行；维护窗口前应先关闭计划。

原目录继续挂载时，设置、任务历史、管理密码、密钥、证书和录像会保留。容器重建不会保留只在内存中的临时登录。新版兼容旧版纯文本 `admin-token`，网页改密码后以 JSON 保存；回退旧程序时恢复更新前的管理密码文件，保留与密文配套的原密钥。

手动复制的证书不会自动跟随 DSM 续期更新，需单独维护。不要用删除密钥、清空数据目录或删除卷作为普通更新步骤。

## 六、常见状态与诊断

| 提示 | 检查方向 |
|---|---|
| 目录不存在 | NAS 路径是否存在，挂载冒号两侧是否正确，项目是否重新部署 |
| 无法进入 / 目录不可写 | UID/GID、目录所有者、权限和 DSM ACL |
| 无法收紧目录权限 | 专用目录所有者是否与运行 UID 一致，不能只增加组读写 |
| 账号临时登录，保存失败 | 页面中的配置 / 密钥 / 密文具体失败步骤与 errno；修复后再次保存 |
| HTTP 禁止短信登录或改密码 | 配置容器原生 TLS，用匹配域名和可信证书访问 |
| 设备配置已准备，设备未检测 | 只通过本地字段检查，实际连接与回调需运行验证 |
| 回调超时 | 设备在线、IP、端口、防火墙及 MQTT；普通网络失败不会自动退出账号 |
| 空间不足 | 释放空间或更换容量充足的挂载，至少保留 5 GiB |

诊断页的权限初检与临时文件检查不代表设备在线、厂商会话有效或原片导出成功。日志只记录固定故障类别、步骤与 errno，不输出凭据。任务历史保存最近 100 轮，中断记录显示“结果未确认”。损坏的历史文件保留，并阻止新的导出。

旧单次入口仍在 `deploy/compose.once.yaml`，属于高级兼容方式；不要与网页服务同时控制设备。主流用户使用本指南的网页服务即可。本版通过本地回归验证，新的群晖端到端导出仍需上述实机验收。
