# NVIDIA 容器工具包内网离线安装操作手册

本文档用于在**完全无法访问互联网**的 Linux 服务器上离线安装 NVIDIA Container Toolkit，使 Docker 具备访问服务器 NVIDIA GPU 的基础能力，并使用标准 CUDA 容器完成验证。

本文只覆盖 Docker 的 GPU 容器运行环境。NVIDIA 显卡驱动必须提前安装并能正常工作；安装 NVIDIA Container Toolkit 不会安装或修复显卡驱动。

由于目标服务器无法访问互联网，需要先在可联网的 Linux 环境中从 NVIDIA 官方软件源下载与目标服务器匹配的安装包和验证镜像，再通过允许的方式导入内网。

如果内部已经提供可信的 APT/YUM 软件源和容器镜像仓库，可以直接从内部源安装 Toolkit、获取 CUDA 验证镜像，并跳过本文第 4～9 节中的介质整理和跨网导入步骤。

## 1. 最终验收标准

以下两项全部通过才算完成：

1. 宿主机执行 `nvidia-smi` 能看到显卡、驱动版本和显存。
2. Docker 使用 `--gpus all` 启动 CUDA 验证容器后，容器内执行 `nvidia-smi` 成功。

## 2. 安装流程概览

安装过程涉及两个环境：

- **联网下载环境**：用于下载 Toolkit 安装包、依赖包和 CUDA 验证镜像，并生成校验文件。
- **内网目标服务器**：核对系统信息和文件校验值，安装本地软件包，配置 Docker runtime，导入验证镜像并完成 GPU 透传验证。

联网下载环境必须与目标服务器满足以下条件：

- Linux 发行版及大版本一致，例如均为 Ubuntu 22.04，或均为 Rocky Linux 9。
- CPU 架构一致，例如均为 `x86_64`/`amd64`，或均为 `aarch64`/`arm64`。
- 建议使用一台全新的同版本虚拟机下载软件包，避免该环境因为已经安装过依赖而漏下载目标服务器所需的依赖。

不要直接在 Windows 或不同 Linux 发行版上下载若干零散包后尝试安装。

## 3. 在内网目标服务器采集环境信息

先在目标服务器执行：

```bash
cat /etc/os-release
uname -m
docker version
nvidia-smi
```

先记录以下信息，再据此下载匹配的安装介质：

- 发行版名称和版本，例如 Ubuntu 22.04、Ubuntu 24.04、Rocky Linux 9。
- CPU 架构，例如 `x86_64` 或 `aarch64`。
- Docker Engine 版本。
- NVIDIA 驱动版本，以及 `nvidia-smi` 显示的最高 CUDA 兼容版本。

### 3.1 前置条件

- `nvidia-smi` 必须成功。若命令不存在或出现 `couldn't communicate with the NVIDIA driver`，应先离线安装或修复 NVIDIA 驱动；继续安装 Toolkit 不能解决驱动问题。
- Docker 服务必须已经可以正常启动普通容器。

## 4. 准备离线安装介质

可采用以下目录结构整理下载的安装介质：

```text
nvidia-container-toolkit-offline/
├── host-info.txt
├── SHA256SUMS
├── packages/
│   ├── deb/                 # Ubuntu/Debian 使用
│   └── rpm/                 # Rocky/RHEL/CentOS 使用
└── images/
    └── nvidia-cuda-validation.tar
```

`host-info.txt` 中至少写明目标发行版、CPU 架构、下载日期和下载环境信息，便于后续追溯。

只需要根据目标服务器的发行版准备 `deb` 或 `rpm` 中的一种。

## 5. Ubuntu/Debian：下载离线安装介质

以下命令在与目标服务器**同发行版、同大版本、同架构**的联网 Linux 环境中执行。

### 5.1 配置 NVIDIA 官方软件源

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates gnupg

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor --yes \
  -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
```

### 5.2 下载 Toolkit 及依赖

推荐使用一台全新的同版本虚拟机执行以下命令，确保依赖没有因“本机已安装”而被漏掉：

```bash
mkdir -p "$HOME/nvidia-container-toolkit-offline/packages/deb"

sudo apt-get install -y --download-only \
  -o Dir::Cache::archives="$HOME/nvidia-container-toolkit-offline/packages/deb" \
  nvidia-container-toolkit
```

确认目录中至少包含以下 NVIDIA 包；实际文件名会带版本号和架构：

```text
libnvidia-container1_*.deb
libnvidia-container-tools_*.deb
nvidia-container-toolkit-base_*.deb
nvidia-container-toolkit_*.deb
```

如果联网下载环境不是全新系统，可显式重新下载上述四个包：

```bash
cd "$HOME/nvidia-container-toolkit-offline/packages/deb"
apt-get download \
  libnvidia-container1 \
  libnvidia-container-tools \
  nvidia-container-toolkit-base \
  nvidia-container-toolkit
```

显式下载只能保证四个 NVIDIA 包齐全，不能保证目标服务器缺少的系统依赖也齐全。因此仍然优先使用全新的同版本联网环境。

## 6. Rocky/RHEL/CentOS：下载离线安装介质

以下命令在与目标服务器**同发行版、同大版本、同架构**的联网 Linux 环境中执行。

### 6.1 配置 NVIDIA 官方软件源

```bash
curl -sL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
  | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo

sudo dnf install -y dnf-plugins-core
```

### 6.2 下载 Toolkit 及全部依赖

```bash
mkdir -p "$HOME/nvidia-container-toolkit-offline/packages/rpm"

dnf download \
  --resolve \
  --alldeps \
  --destdir "$HOME/nvidia-container-toolkit-offline/packages/rpm" \
  nvidia-container-toolkit
```

确认目录中至少包含以下 NVIDIA 包；实际文件名会带版本号和架构：

```text
libnvidia-container1-*.rpm
libnvidia-container-tools-*.rpm
nvidia-container-toolkit-base-*.rpm
nvidia-container-toolkit-*.rpm
```

`--resolve --alldeps` 会把联网环境认为需要的依赖一并下载。仍建议使用干净的同版本虚拟机下载这些软件包。

## 7. 下载 CUDA 验证镜像

目标服务器不能访问 Docker Hub，因此必须提前把验证镜像打包。

在安装了 Docker 的联网 Linux 机器上执行：

```bash
mkdir -p "$HOME/nvidia-container-toolkit-offline/images"

docker pull nvidia/cuda:12.8.0-base-ubuntu22.04

docker save \
  -o "$HOME/nvidia-container-toolkit-offline/images/nvidia-cuda-validation.tar" \
  nvidia/cuda:12.8.0-base-ubuntu22.04
```

验证镜像的 CUDA 版本不要求与 `nvidia-smi` 顶部显示的版本完全相同，但镜像所需 CUDA 版本不能超过宿主机驱动支持范围。若服务器驱动较旧，应选择更低版本的 `nvidia/cuda` 基础镜像，并在后续所有命令中使用相同标签。

对于 ARM 服务器，必须确认镜像标签支持 `linux/arm64`。可在联网环境检查：

```bash
docker buildx imagetools inspect nvidia/cuda:12.8.0-base-ubuntu22.04
```

## 8. 生成校验文件并导入内网

在联网下载环境执行：

```bash
cd "$HOME/nvidia-container-toolkit-offline"
find packages images -type f -print0 \
  | sort -z \
  | xargs -0 sha256sum > SHA256SUMS

cd "$HOME"
tar -czf nvidia-container-toolkit-offline.tar.gz \
  nvidia-container-toolkit-offline
sha256sum nvidia-container-toolkit-offline.tar.gz \
  > nvidia-container-toolkit-offline.tar.gz.sha256
```

通过允许的介质或文件传输系统，把以下两个文件导入内网目标服务器：

```text
nvidia-container-toolkit-offline.tar.gz
nvidia-container-toolkit-offline.tar.gz.sha256
```

传输介质进入内网前应按单位安全制度进行病毒扫描、审批和登记。不要使用来源不明的软件包或镜像归档。

## 9. 在内网目标服务器核验离线包

假设文件已放到 `/opt/offline`：

```bash
cd /opt/offline
sha256sum -c nvidia-container-toolkit-offline.tar.gz.sha256

tar -xzf nvidia-container-toolkit-offline.tar.gz
cd nvidia-container-toolkit-offline
sha256sum -c SHA256SUMS
```

任何一项显示 `FAILED` 都必须停止安装，重新导入或重新下载安装介质。

## 10. Ubuntu/Debian：在内网服务器安装

```bash
cd /opt/offline/nvidia-container-toolkit-offline/packages/deb
sudo apt-get install -y ./*.deb
```

如果 APT 报缺少依赖，不要执行联网修复命令，也不要使用 `--fix-broken` 删除现有软件。记录缺少的包名和版本，在同版本联网环境补齐对应 `.deb` 后重新导入内网。

确认安装结果：

```bash
dpkg -l | grep -E 'nvidia-container|libnvidia-container'
nvidia-ctk --version
```

## 11. Rocky/RHEL/CentOS：在内网服务器安装

```bash
cd /opt/offline/nvidia-container-toolkit-offline/packages/rpm
sudo dnf install -y --disablerepo='*' ./*.rpm
```

如果 DNF 报缺少依赖，记录缺少的包名和版本，在同版本联网环境使用 `dnf download --resolve --alldeps` 补齐后重新导入内网。

确认安装结果：

```bash
rpm -qa | grep -E 'nvidia-container|libnvidia-container'
nvidia-ctk --version
```

## 12. 配置 Docker 使用 NVIDIA runtime

以下步骤适用于由 root 管理的普通 Docker 服务。

先备份 Docker daemon 配置：

```bash
sudo mkdir -p /etc/docker

if sudo test -f /etc/docker/daemon.json; then
  sudo cp -a /etc/docker/daemon.json \
    "/etc/docker/daemon.json.before-nvidia.$(date +%Y%m%d%H%M%S)"
fi
```

让 NVIDIA 工具把 runtime 配置合并到 `/etc/docker/daemon.json`：

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo cat /etc/docker/daemon.json
```

检查 Docker 配置并重启服务：

```bash
sudo dockerd --validate --config-file=/etc/docker/daemon.json
sudo systemctl restart docker
sudo systemctl --no-pager --full status docker
```

如果系统中的 `dockerd` 不支持 `--validate`，可跳过该条，但必须在重启后检查 Docker 服务状态和日志：

```bash
sudo journalctl -u docker -n 100 --no-pager
```

确认 NVIDIA runtime 已注册：

```bash
docker info | sed -n '/Runtimes/p'
```

输出中应包含 `nvidia`。不需要把 `nvidia` 设置为所有容器的默认 runtime；需要使用 GPU 的容器可在启动时通过 `--gpus` 单独请求。

## 13. 导入镜像并验证 Docker GPU 透传

导入下载好的验证镜像：

```bash
docker load \
  -i /opt/offline/nvidia-container-toolkit-offline/images/nvidia-cuda-validation.tar

docker image inspect nvidia/cuda:12.8.0-base-ubuntu22.04 \
  --format '{{.Id}} {{.Architecture}}'
```

执行 GPU 验证：

```bash
docker run --rm --gpus all \
  nvidia/cuda:12.8.0-base-ubuntu22.04 \
  nvidia-smi
```

预期结果：容器内正常显示与宿主机一致的 GPU 型号、数量和驱动版本。至此，服务器的 Docker GPU 基础能力验收完成。若失败，按第 14 节排障。

## 14. 常见故障排查

### 14.1 宿主机 `nvidia-smi` 失败

原因通常是驱动未安装、驱动内核模块未加载、内核升级后模块未重新构建，或者 Secure Boot 阻止模块加载。

检查：

```bash
lspci | grep -i nvidia
lsmod | grep nvidia
dmesg | grep -iE 'nvidia|nouveau' | tail -n 100
```

这是宿主机驱动问题，与 Container Toolkit 无关。

### 14.2 Docker 提示 `could not select device driver` 或 `unknown runtime nvidia`

检查：

```bash
nvidia-ctk --version
sudo cat /etc/docker/daemon.json
docker info | sed -n '/Runtimes/p'
sudo journalctl -u docker -n 100 --no-pager
```

重新配置并重启：

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### 14.3 提示 `libnvidia-ml.so`、驱动库或设备文件缺失

先确认宿主机 `nvidia-smi` 正常，再检查：

```bash
ldconfig -p | grep libnvidia-ml
ls -l /dev/nvidia*
nvidia-container-cli info
```

若宿主机库不完整，应修复与当前内核和显卡匹配的 NVIDIA 驱动包。

### 14.4 CUDA 镜像提示驱动版本不足

通常是验证镜像所需 CUDA 版本高于宿主机驱动支持范围。查看宿主机 `nvidia-smi`，在联网环境换用更低版本的 CUDA 基础镜像，重新 `docker save` 并导入内网。

## 15. 回滚

### 15.1 回滚 Docker runtime 配置

找到第 12 节生成的备份：

```bash
sudo ls -l /etc/docker/daemon.json.before-nvidia.*
```

确认要恢复的文件后执行：

```bash
sudo cp -a /etc/docker/daemon.json.before-nvidia.YYYYMMDDHHMMSS \
  /etc/docker/daemon.json
sudo systemctl restart docker
```

如果安装前不存在 `/etc/docker/daemon.json`，先检查当前文件内容。若其中只有本次生成的 `runtimes.nvidia` 配置，可以把该文件移走后重启 Docker；若还有镜像仓库、日志、存储驱动等其他配置，只删除 `runtimes` 下的 `nvidia` 项，不能覆盖或删除其他配置。修改后先执行 `dockerd --validate`，再重启 Docker。

### 15.2 卸载 Toolkit

Ubuntu/Debian：

```bash
sudo apt-get remove -y \
  nvidia-container-toolkit \
  nvidia-container-toolkit-base \
  libnvidia-container-tools \
  libnvidia-container1
```

Rocky/RHEL/CentOS：

```bash
sudo dnf remove -y \
  nvidia-container-toolkit \
  nvidia-container-toolkit-base \
  libnvidia-container-tools \
  libnvidia-container1
```

卸载 Toolkit 不应卸载 NVIDIA 显卡驱动。执行卸载前应阅读包管理器给出的依赖变更列表，避免误删其他软件。

## 16. 安装记录建议

安装完成后保存以下信息：

```bash
cat /etc/os-release
uname -m
nvidia-smi
nvidia-ctk --version
docker version
docker info
docker run --rm --gpus all \
  nvidia/cuda:12.8.0-base-ubuntu22.04 \
  nvidia-smi
```

同时归档：

- 离线包及 `SHA256SUMS`。
- Toolkit 软件包版本。
- CUDA 验证镜像名称、标签和镜像 ID。
- `/etc/docker/daemon.json` 修改前后的版本。
- 安装时间、操作人、服务器资产编号和验收结果。

## 17. 参考资料

- [NVIDIA Container Toolkit 官方安装指南](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)：介绍支持的平台、软件源配置、安装命令和 Docker runtime 配置。
- [NVIDIA Container Toolkit 示例工作负载](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html)：提供标准 CUDA 容器验证示例，可用于确认 Docker 是否已经具备 GPU 访问能力。
- [NVIDIA Container Toolkit 故障排查](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/troubleshooting.html)：安装成功但容器无法使用 GPU 时，优先参考此页面。
- [NVIDIA Container Toolkit 版本发布记录](https://github.com/NVIDIA/nvidia-container-toolkit/releases)：查询 Toolkit 最新版本、历史版本和变更说明。
- [NVIDIA CUDA 容器镜像说明](https://hub.docker.com/r/nvidia/cuda)：查询可用 CUDA 镜像标签。下载验证镜像前，应确认标签真实存在并与服务器架构、驱动兼容。
- [NVIDIA CUDA 驱动兼容性说明](https://docs.nvidia.com/deploy/cuda-compatibility/)：解释显卡驱动版本与 CUDA 版本之间的兼容关系。
- [Docker GPU 资源使用说明](https://docs.docker.com/engine/containers/resource_constraints/#gpu)：说明 Docker 使用 `--gpus` 参数访问 GPU 的基本方法和前置条件。
- [Docker `run --gpus` 参数参考](https://docs.docker.com/reference/cli/docker/container/run/#access-an-nvidia-gpu)：介绍全部 GPU、指定数量 GPU 和指定设备编号的启动方式。
