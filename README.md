# aws-proxy

在已有的 Ubuntu Lightsail 实例上部署 Xray VLESS/WebSocket 代理，主要面向中国大陆网络中的客户端。服务器发现可用的 CloudFront 边缘 IP，并通过另一套 CloudFront 分配（distribution）和私有 S3 存储桶发布 Mihomo / Clash Verge Rev 订阅。

项目依赖 AWS Lightsail、CloudFront、S3 和 IAM。账户、源站、节点发现和 Clash 行为通过 JSON 文件配置。示例使用东京，也可配置其他 AWS 区域。**当前版本仅部署 CloudFront 链路。** 中国大陆的实际可用性取决于客户端运营商和路由，需要在使用地点验证。

## 工作方式

```text
Clash ── TLS + VLESS/WebSocket ── 代理 CloudFront ── HTTPS ── Nginx ── Xray
  │
  └── HTTPS 订阅 / 节点列表 ── 订阅 CloudFront ── 私有 S3
                                                  ▲
                          源站每小时运行的发布程序 ──┘
```

- 所有 CloudFront 候选节点放在同一个 `proxy-provider` 中，不在完整配置里重复嵌入引导节点。
- 只有边缘 IP 变化时，完整配置保持不变。完整配置和节点列表都仅在文件内容变化时上传。
- 客户端默认每小时刷新节点列表。完整订阅建议在 Clash Verge Rev 界面中设为每天刷新一次。
- `AUTO` 从客户端网络测试节点；`PROXY` 允许手动选择 `AUTO`、候选节点、可选的直连代理节点或 `DIRECT`。
- 候选列表为空时，`AUTO` 拒绝连接，不会自动改为直连。需要支持 `empty-fallback` 的 Mihomo 版本，不支持旧版 Clash。
- Xray 使用上游发布的二进制文件，下载时固定版本并校验 SHA256。本仓库包含部署和发布程序源码，不包含 Xray-core 的源码副本。

### Python 环境和服务器进程

Lightsail 上的 Ubuntu 支持 Python 3。初始化脚本安装 `python3-boto3` 及其 Python 依赖，发布程序使用 `/usr/bin/python3`。部署命令在管理电脑或 CloudShell 中执行，不会常驻服务器。

| 组件 | Lightsail 实例上的进程 |
| --- | --- |
| Xray | 1 个常驻进程，处理 VLESS/WebSocket |
| Nginx | 1 个常驻主进程，加上配置的工作进程 |
| 发布程序 | 每小时启动 1 个 Python 进程，外加 `flock` 包装进程和最多 4 个并行 curl 探测进程；完成后退出 |
| Certbot | 定期检查和续签证书，完成后退出 |
| CloudFront、S3、IAM | AWS 托管服务，不占用实例上的进程 |

不计操作系统服务，平时有两个常驻应用服务，总进程数为 **2 + Nginx 工作进程数**。例如 Nginx 有 2 个工作进程时，共有 4 个常驻应用进程。Systemd 定时器在等待期间不会让 Python 或 Certbot 常驻。保留另行安装的直连 REALITY 服务会再增加一个 Xray 进程；本仓库不安装该服务。

## 文件与配置

| 文件 | 用途 |
| --- | --- |
| [config/deployment.example.json](config/deployment.example.json) | AWS 账户和区域、SSH 目标、源站证书、节点发现和客户端刷新设置 |
| [config/clash.example.json](config/clash.example.json) | 客户端端口、IPv6、DNS、Loyalsoldier 规则顺序、GEOIP 和可选手动代理 |
| [deploy.py](deploy.py) | 创建 AWS 资源、记录部署状态、通过 SSH 部署和验证 |
| [server.py](server.py) | 生成 Ubuntu 初始化脚本、Xray/Nginx 配置和 systemd 单元 |
| [publisher.py](publisher.py) | 发现候选 IP、验证 TLS 和健康响应、生成节点列表并按需上传 S3 |

将两个示例分别复制为 `config/deployment.json` 和 `config/clash.json`，再把 `clash.base_file` 改为 `clash.json`。该路径相对于部署配置文件所在目录解析。Git 默认忽略 `config/` 下非示例的 JSON 文件以及 `.state/`；不要把真实配置写入会提交的 `*.example.json`。

Clash 基础配置使用 JSON，也是合法的 YAML。`proxy-groups` 和 `proxy-providers` 可以留空或省略，由程序生成，并依次放在 `proxies` 后面，三者是同级字段。其他字段顺序和规则顺序保留，只在规则最前面增加 `DOMAIN,<订阅分配域名>,DIRECT`，用于直接获取订阅。程序根据 `subscription_dns` 添加该域名的 DNS 策略；规则提供器未指定下载代理时，默认使用自动选择组。修改手动组名称时，也要修改规则目标。

示例使用 7890 端口、禁用 IPv6，并保留面向大陆网络的 DNS 和 Loyalsoldier/GEOIP 规则顺序。`gfw` 和 `tld-not-cn` 已声明但未被规则引用，不需要时可删除。TUN 开关和界面覆盖选项仍由客户端管理。

可以在基础配置的 `proxies` 列表中加入已有的直连代理节点。本项目只安装 CloudFront WebSocket 服务，不创建或删除直连 REALITY 服务。直连节点凭据应放在被 Git 忽略的本地配置中；如需绕过代理访问该节点，也应自行添加对应规则。

## deployment.example.json 字段说明

这些字段是本项目的配置接口，不是可以原样提交给 AWS 的 API 参数。当前加载器要求保留示例中的字段，没有自动补全默认值的机制。下表的“示例值”来自示例文件，不代表所有部署都应该使用该值。

### 项目标识与 AWS

| 字段 | 示例值 | 含义与填写方式 |
| --- | --- | --- |
| `project` | `aws-proxy` | 本次部署的本地名称，用于资源名称前缀、`/opt/<project>` 目录和 systemd 服务名。长度 3～24，只能使用小写字母、数字和连字符，必须以字母开头。它不是 AWS 控制台中的 Project 对象。 |
| `aws.account_id` | `123456789012` | 目标 AWS 账户的 12 位 ID，需替换为真实值。部署前通过 STS 核对当前凭据所属账户，防止操作错误账户。它不是 Access Key，也不是 Builder ID。 |
| `aws.region` | `ap-northeast-1` | 部署使用的 AWS 区域，当前代码主要用它创建和访问 S3 存储桶；示例为东京。Lightsail 目标由 SSH 配置决定，改此值不会迁移或创建实例，也不会指定 CloudFront 边缘节点。当前支持标准商业 AWS 区域，不支持 AWS 中国或 GovCloud 分区。 |
| `aws.price_class` | `PriceClass_All` | 两个 CloudFront 分配使用的价格级别，影响可选边缘位置范围。本项目接受 `PriceClass_All`、`PriceClass_200`、`PriceClass_100`。它不是预算上限，也不能固定某个 POP。 |

`PriceClass_All` 不通过价格级别缩小边缘位置范围；其他级别限制可选范围，可能影响延迟。参见 [AWS PriceClass 说明](https://docs.aws.amazon.com/cloudfront/latest/APIReference/API_DistributionConfig.html#cloudfront-Type-DistributionConfig-PriceClass)。

### server：Lightsail 源站

| 字段 | 示例值 | 含义与填写方式 |
| --- | --- | --- |
| `server.ssh_target` | `ubuntu@203.0.113.10` | 管理端传给 `ssh` 的目标，可以是 `用户@主机` 或 `~/.ssh/config` 中的别名。密钥和自定义 SSH 端口在 SSH 配置中设置，不要在此字段拼接命令参数。 |
| `server.public_ip` | `203.0.113.10` | 源站固定公网 IPv4，替换为 Lightsail 绑定的静态 IP。当前发布程序用它构造 DNS 客户端子网提示；它不会自动设置 SSH 目标、DNS 记录或 Clash 节点地址。 |
| `server.origin_hostname` | `origin.example.com` | CloudFront 连接源站时使用的 DNS 名称，也是 Nginx 虚拟主机和证书的域名。必须解析到源站并能申请公开信任的证书。它不是订阅域名，也不能填写带 `https://` 或端口的 URL。 |
| `server.certificate_email` | `admin@example.com` | 申请证书时交给 Certbot/ACME 账户使用的邮箱，替换为自己的可用邮箱。 |
| `server.https_port` | `8443` | Nginx 接收 CloudFront HTTPS 回源的端口，需在 Lightsail 和服务器防火墙中允许相应访问。客户端连接 CloudFront 仍使用 443。 |
| `server.ws_port` | `18081` | Xray 内部 WebSocket 端口，仅绑定 `127.0.0.1`，由 Nginx 转发，不需要对公网开放。必须与 `https_port` 不同；本项目要求两者均为 1024～65535 的整数。 |
| `server.xray_version` | `REPLACE_WITH_RELEASE_TAG` | Xray 上游完整发布标签，含 `v` 前缀。部署前替换为选定版本，不要使用 `latest`。 |
| `server.xray_archive` | `Xray-linux-64.zip` | 发布文件名。x86_64 使用示例值；ARM64 使用 `Xray-linux-arm64-v8a.zip`。初始化脚本检查服务器架构是否匹配。 |
| `server.xray_sha256` | `REPLACE_WITH_64_HEX_SHA256` | 所选版本、所选 ZIP 的 SHA256，必须为 64 位十六进制字符串。通过可信来源核对后填写，下载校验通过才会安装。不是解压后可执行文件的哈希。 |

`203.0.113.10`、`example.com` 下的域名和示例邮箱仅用于文档，不能直接部署。SSH 目标、静态 IP 和源站域名应指向同一台服务器；脚本不会配置这些关联。证书申请和续签还需要 TCP 80，这个端口没有单独的配置项。

### discovery：候选边缘 IP 的发现与筛选

| 字段 | 示例值 | 含义与填写方式 |
| --- | --- | --- |
| `discovery.pop_prefixes` | `["NRT", "HND"]` | 允许的 POP 三字母前缀。程序读取探测响应的 `x-amz-cf-pop`，只保留匹配前缀的候选。示例筛选东京相关的 NRT/HND，不限制为某两个完整 POP 编号。至少填写一个前缀。 |
| `discovery.hint_regions` | `["ap-northeast-1"]` | 从 AWS 公布的 IP 范围中选取哪些区域的 EC2 地址，生成 Google DoH 查询的 EDNS Client Subnet 提示。用于扩大候选来源，不保证返回该区域，也不会创建 EC2 实例。 |
| `discovery.seed_ips` | `[]` | 手工加入探测池的公网 IPv4，例如从客户端网络测得可用的边缘地址。仍需通过 TLS、健康响应和 POP 检查，不会直接发布。可留空。 |
| `discovery.preferred_ips` | `[]` | 同时加入探测池并优先考虑的公网 IPv4，列表顺序参与最终排序。仍要通过检查并兼顾不同 POP 的覆盖，不保证全部入选。可留空。 |
| `discovery.excluded_ips` | `[]` | 排除的公网 IPv4，即使来自 DNS、种子或上次结果也不选用。排除优先于偏好。可留空。 |
| `discovery.max_candidates` | `8` | 最终发布节点数的上限，允许 1～32。可用节点不足时发布实际数量；一个都没有时失败并保留上次发布内容。 |
| `discovery.probe_budget` | `64` | 每轮最多探测多少个去重后的公网 IPv4，不得小于 `max_candidates`，且不超过 64。这是数量限制，不是金额预算。当前并行度固定为 4。 |

`aws.region`、`hint_regions` 和 `pop_prefixes` 分别控制 AWS 部署区域、DNS 提示来源和最终接受的 POP，可以分别设置。服务器端探测不能证明 IP 从北京可达，最终仍由客户端健康检查选择节点。

### clash：生成的客户端配置

| 字段 | 示例值 | 含义与填写方式 |
| --- | --- | --- |
| `clash.base_file` | `clash.example.json` | Clash 基础文件路径，相对于部署配置所在目录解析。使用本地配置时通常改为 `clash.json`。该文件配置端口、IPv6、DNS、规则和手动代理。 |
| `clash.provider_name` | `EdgeCandidates` | 生成的节点提供器名称，供代理组通过 `use` 引用，也用于缓存路径 `./providers/<名称>.yaml`。可命名为 `TokyoCandidates`。 |
| `clash.select_group` | `PROXY` | 手动选择组名称，包含自动组、基础文件中的手动节点、`DIRECT` 和候选节点。改名时要同步修改基础文件中指向它的规则。 |
| `clash.auto_group` | `AUTO` | 自动选择组名称，使用 `url-test` 检查候选节点。规则提供器未明确指定下载代理时，也使用这个组。 |
| `clash.provider_interval` | `3600` | 客户端重新下载 `nodes.yaml` 的间隔，单位为秒；3600 表示每小时。不改变服务器发现周期，也不设置完整订阅刷新周期。 |
| `clash.health_url` | `https://www.gstatic.com/generate_204` | 客户端通过候选代理访问的健康检查 URL，同时用于节点提供器和自动组。应选择稳定、响应较小的 HTTPS 地址。它不同于服务器探测 CloudFront 时使用的 `/health`。 |
| `clash.health_status` | `204` | 客户端健康检查期望的 HTTP 状态码，要求 100～599 的整数。更换检测 URL 时应同步核对正常返回值。 |
| `clash.health_interval` | `300` | 客户端健康检查间隔，单位为秒；300 表示每 5 分钟。不影响节点列表下载频率。生成配置使用 `lazy: false`。 |
| `clash.health_timeout_ms` | `5000` | 客户端一次健康检查的超时时间，单位为毫秒；5000 表示 5 秒。不控制服务器端 curl 探测超时。 |
| `clash.tolerance_ms` | `50` | 自动组切换节点时的延迟容差，单位为毫秒，用于减少小幅延迟波动引起的频繁切换。要求非负整数。 |
| `clash.subscription_dns` | `["223.5.5.5", "119.29.29.29"]` | 加入 Clash `dns.nameserver-policy`、专门解析订阅 CloudFront 域名的 DNS 列表。不替换所有 DNS 设置，也不配置服务器 DNS。设为 `[]` 时不额外添加该域名策略。 |

提供器名、手动组名和自动组名必须互不相同，长度 1～41，以英文字母开头，后续可用字母、数字、下划线或连字符。两个组名不能使用 `DIRECT` 或 `REJECT`。参见 [Mihomo 节点提供器说明](https://wiki.metacubex.one/en/config/proxy-providers/) 和 [URL-test 容差说明](https://wiki.metacubex.one/en/config/proxy-groups/url-test/#tolerance)。

### 哪些设置会随部署固定

`project`、整个 `aws` 对象和整个 `server` 对象都会记录到部署状态中。当前实现拒绝用修改后的这些值继续操作同一份状态，包括修改 SSH 别名或 Xray 版本。新部署应使用新的项目名和状态目录；已有部署的基础设施变更需要单独设计迁移步骤。

`discovery`、`clash` 和 Clash 基础文件可以修改后重新执行 `publish`。服务器发现周期目前固定为每小时，并带最多 60 秒随机延迟；完整订阅的每日刷新在 Clash Verge Rev 界面设置。这两项不在 JSON 中。

VLESS UUID、WebSocket 路径、源站鉴权令牌、订阅随机路径、S3 桶名和 CloudFront 分配 ID/域名由程序生成并保存在私有状态中，无需手填示例。AWS 登录凭据通过 Boto3 标准凭据链获取，SSH 密钥通过 SSH 配置提供。

## 部署前准备

1. 已有 Ubuntu 22.04/24.04 Lightsail 实例，绑定静态公网 IPv4，能访问互联网。实例创建、静态 IP 分配和防火墙设置需手工完成。
2. 已有解析到该 IP 的源站域名，并满足公开证书申请条件。优先使用自己控制的域名；基于 IP 的 DNS 服务也可使用，但需考虑其可用性和证书申请限制。
3. TCP 80 可用于 ACME 验证和续签；HTTPS 回源端口（默认 8443）允许 CloudFront 访问。内部 WebSocket 端口不对公网开放，SSH 仅允许管理来源访问。脚本不修改防火墙。
4. SSH 密钥登录、免密码 sudo，以及已核验并写入 `known_hosts` 的主机密钥。接受密钥前，通过可信控制台渠道核对指纹。自定义端口和密钥在 `~/.ssh/config` 配置，不要关闭主机密钥验证。
5. 使用 Linux、WSL 或 AWS CloudShell 部署，准备 Python 3.10+、OpenSSH 和目标账户凭据。Boto3 使用标准凭据链，支持通过 `AWS_PROFILE` 选择配置。部署身份需要执行 `deploy.py` 中的 S3、CloudFront、IAM 和 STS 操作；服务器发布程序使用另一个权限受限的 IAM 密钥。
6. 选择明确的 [Xray 发布版本](https://github.com/XTLS/Xray-core/releases)、匹配架构的 ZIP 和经核验的 SHA256，填入配置。占位值会让初始化脚本拒绝执行。

建议使用新实例。初始化会安装 Nginx、Certbot、curl、Python/boto3 和 Xray，并拒绝覆盖已有 Xray 二进制文件。若依赖及 `xray` 系统用户已存在，应先确认兼容性，再跳过 `bootstrap`。源站部署会写入以 `project` 命名的文件，避免与已有虚拟主机、端口和服务冲突。

## 部署步骤

在仓库目录运行。这些命令会创建计费 AWS 资源，并在申请源站证书时接受证书颁发机构条款。工具不管理 AWS 赠金，也不设置支出上限。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config/deployment.example.json config/deployment.json
cp config/clash.example.json config/clash.json
# 编辑两个本地文件，并将 clash.base_file 改为 clash.json。
python deploy.py check
python deploy.py bootstrap    # 仅用于需要初始化的服务器
python deploy.py hosting      # 私有 S3、订阅 CloudFront 和 OAC
python deploy.py origin       # 证书、Nginx/Xray 和代理 CloudFront
python deploy.py status       # 等待两个分配均显示 Deployed
python deploy.py publish      # 安装并运行发布程序，输出私密订阅 URL
python deploy.py verify       # 验证服务、S3、CloudFront，并模拟证书续签
```

`check` 仅执行离线检查，不安装依赖。任一分配尚未部署完成时，`publish` 会拒绝继续。找不到通过验证的候选节点时，不覆盖上次发布文件；首次部署出现这种情况，表示订阅还未就绪，应检查日志和发现配置后重试。

将输出的 URL 导入 Clash Verge Rev 远程配置，完整订阅刷新间隔设为一天，并选择 `PROXY → AUTO`。从实际网络测试 Google、GitHub 和所需服务后再正式使用。内置服务器验证不能证明客户端可达或 VLESS 流量正常。

另一套部署的每条命令都应附带 `--config config/another.json --state-dir .state/another`。状态目录应放在 `.state/` 下或 Git 仓库之外，并保留加密备份；其中包含恢复所需的资源 ID 和凭据。

## 节点发现与更新

服务器综合使用系统 DNS、Google DoH（包括基于指定 AWS 区域地址生成的 EDNS 客户端子网提示）、种子 IP 和上次发布的 IP。每轮最多探测 64 个公网 IPv4，并行度为 4，检查分配域名的 TLS 证书、准确的健康响应内容以及允许的 `x-amz-cf-pop` 前缀。默认最多发布 8 个候选，尽量覆盖本次观察到的不同 POP。

这是尽力发现候选的算法，不会遍历全部 AWS 边缘节点，也不代表北京线路实测。服务器可达不等于大陆客户端可达；同一 IP 所服务的 POP 也可能变化。客户端的 `AUTO` 检查负责选择可用节点。若结果都不在允许的位置中，可加入经过客户端实测的种子 IP，或调整允许的 POP。客户端应保留上次可用的订阅和节点缓存。

修改发现设置、Clash 基础配置或发布程序源码后，运行：

```bash
python deploy.py check
python deploy.py publish
```

更新复用已安装的发布密钥并重新启动发布任务。VLESS UUID、WebSocket 路径和订阅前缀保持稳定，**每小时发现节点不会轮换凭据**。变更文件可能被 CloudFront 缓存最多 5 分钟，之后还需等待客户端刷新。服务器的 `status.json` 记录最近成功发布时间和本次实际上传的文件名。

在服务器上查看状态或手动运行（替换为实际项目名）：

```bash
sudo systemctl status aws-proxy.service aws-proxy-publish.timer
sudo journalctl -u aws-proxy-publish.service -n 50 --no-pager
sudo cat /opt/aws-proxy/status.json
sudo systemctl start aws-proxy-publish.service
sudo python3 /opt/aws-proxy/publisher.py --root /opt/aws-proxy --discover-only
```

`--discover-only` 会探测网络，但不写入 S3。失败时保留上次发布内容，应留意最近成功时间是否过旧。定时任务有进程锁和执行时限，但没有自动创建监控告警服务。

## 凭据、恢复与清理

订阅桶阻止公开访问，CloudFront OAC（源站访问控制）只允许对应订阅分配读取文件。运行时 IAM 凭据仅能写入两个指定 S3 对象，不能读取或列出桶内容。凭据和生成文件使用受限权限，代理源站关闭 Nginx 访问日志。

订阅 URL 含有随机的 256 位路径。**拿到 URL 就能读取代理凭据**：它难以猜测，但不是用户身份认证。不要放进公开 issue、截图、配置或日志，也不要发布 `.state/`、私钥、生成的订阅或真实本地配置。

资源创建后记录 ID，通常可使用同一状态继续部署。但 AWS 创建和本地状态写入不是同一事务：若在两步之间中断，重试前应检查 AWS 中是否已有资源或密钥，不要通过丢弃状态绕过错误。成功安装后再次 `publish` 会使用服务器上的已有凭据；服务器或密钥丢失时，需手工恢复或轮换 IAM 密钥。

Nginx/Xray 配置在启用前经过检查，但多文件更新不具有事务性。Xray 保留 `server.json.previous` 供手工恢复；在已有服务器上部署前应检查现有配置。S3 旧对象版本保留 7 天，恢复需手工操作。发布程序假定只有它写入这两个对象；手工恢复或删除 S3 对象后，应删除对应本地上传记录文件（`/opt/<project>/clash.yaml` 或 `nodes.yaml`），让下次成功运行重新上传。

清理时逐项操作：停止并禁用项目发布定时器和源站服务；停用并删除 IAM 访问密钥、内联策略和用户；禁用两个 CloudFront 分配，等待变更完成后删除；确认无其他使用者后删除 OAC；清空专用 S3 桶的**所有版本和删除标记**后删除桶。只移除本项目的 Nginx/systemd/Certbot 文件和 `/opt/<project>` 数据。若专用实例不再需要，最后删除实例并释放静态 IP。

状态记录了具体资源 ID，不要用名称通配符删除可能共享的资源。仅停止实例不会清理其他仍可能产生费用的资源。

## 测试与发布仓库

```bash
python -m unittest discover -v
python deploy.py check --config config/deployment.example.json
git status --short
git diff --cached
```

测试覆盖发现失败、POP 分布、区域配置、完整配置稳定性和字段顺序、部分上传失败、IAM 权限范围、私有 S3、WebSocket 转发及生成的 Bash 语法。测试使用模拟对象，不创建 AWS 资源；CI 执行相同测试。它们不能替代全新 AWS 部署、Xray/Mihomo 配置验证或客户端实测。

本地初始化时未配置 GitHub 远程地址。添加远程仓库和推送前检查暂存内容；向第三方开放复用前，选择合适许可证。上游依赖保留各自许可证。

参考文档：[CloudFront WebSocket 支持](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/distribution-working-with.websockets.html)、[Mihomo 节点提供器](https://wiki.metacubex.one/en/config/proxy-providers/)、[Mihomo URL-test 组](https://wiki.metacubex.one/en/config/proxy-groups/url-test/)、[Loyalsoldier 规则](https://github.com/Loyalsoldier/clash-rules)。
