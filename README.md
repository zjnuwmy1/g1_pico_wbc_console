# G1 PICO WBC Console

PICO 4 Ultra 全身遥操作 Unitree G1 的 step-by-step 向导 GUI，
基于 [NVlabs/GR00T-WholeBodyControl](https://github.com/NVlabs/GR00T-WholeBodyControl) (GEAR-SONIC)。

- 上游仓库（本目录只读取/启动它，**绝不修改**）：
  `~/gear_sonic_elbow_crawling_isaacsim/GR00T-WholeBodyControl`
- 零第三方依赖：系统 `python3` 直接跑（stdlib HTTP server + 原生 JS）。

## 启动

```bash
~/Desktop/g1_pico_wbc_console/start_console.sh
# → http://127.0.0.1:8090
```

或双击桌面 `G1-PicoWBC-Console.desktop`。

## 5 步流程

1. **环境检查** — 仓库/模型/二进制/venv/PC Service/网络 卡片。
2. **安装缺失组件** — 一键跑 `install_pico.sh`（建 `.venv_teleop`）；
   PC Service 需 sudo，页面给出复制命令：
   ```bash
   wget '<deb url>' -O /tmp/xrobotoolkit.deb && sudo dpkg -i /tmp/xrobotoolkit.deb
   ```
3. **PICO 连接** — 头显 XRoboToolkit app 填本机 WiFi IP，探测 body data。
4. **Sim 遥操作** — 顺序启动 MuJoCo → deploy(sim) → PICO streamer，
   底栏实时显示 StreamMode / 步态 / CONTROL。
5. **真机遥操作** — 安全确认门（checklist + 输入「确认姿态」→ 120s 单次 token）
   → deploy(real) → streamer。

## 安全设计（重要）

- **急停**：底栏红色 E-STOP → 向部署进程 stdin 写官方急停字符 `o`（策略干净自退），
  再 SIGINT streamer。手柄 A+B+X+Y 等效。
- **真机部署进程绝不 SIGKILL**（本机器人有 arm_sdk 强杀后卡死、只能断电的历史）。
  优雅停止梯度：`o` → SIGINT → SIGTERM → 「需人工处理」横幅。
- `Init Done` 后约 3s 机器人会缓动到默认站姿 —— 所以安全门在**启动部署前**。
- 步骤 5 的安全确认**不持久化**，每次进真机模式都要重新确认。

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | HTTP API + 向导状态 + 安全 token |
| `procman.py` | 进程编排（分级停止策略 / 日志环形缓冲 / stdout 标记解析） |
| `checks.py` | 预检（逻辑复制自 repo 的 launch_pico_sonic_real.py，无运行时耦合） |
| `config.py` | 路径 / 端口 / stdout 标记正则（单一事实源） |
| `helpers/pico_probe.py` | 在 `.venv_teleop` 下探测 xrt body data |
| `logs/<时间戳>/<进程>.log` | 每次运行的完整进程日志 |
| `state/state.json` | 向导步骤进度（不含步骤 5） |

## deploy 二进制修复（2026-06-08，已完成）

仓库自带的 `g1_deploy_onnx_ref` 启动即 `free(): invalid pointer` 崩溃。根因：
编译时自动检测到 ROS jazzy（`HAS_ROS2=1`），运行时 jazzy 的 `libddsc` 抢在
unitree_sdk2 自带 `libddsc` 之前加载，两版 CycloneDDS ABI 不兼容。

**修法**：用 `HAS_ROS2=0` 重新编译（`just build`），二进制只链 unitree libddsc。
重编/运行需要这套环境（已写进 `config.DEPLOY_ENV`，GUI 自动带上）：

```
HAS_ROS2=0
TensorRT_ROOT=~/.local/opt/tensorrt-10.13.3       # 必须 10.13.3，支持 RTX5080 Blackwell sm_120
onnxruntime_ROOT=~/.local/opt/onnxruntime-1.22.0
CUDA_HOME=CUDAToolkit_ROOT=~/.local/opt/cuda-runtime-12.6
MSGPACK_ROOT=~/.local/opt/msgpack-cxx/usr
```

运行加 `--output-type zmq`（无 ROS2 通道）。重编脚本留存于 `/tmp/rebuild_deploy.sh`。
sim 验证：sim_loop + deploy_sim 经 GUI 跑到 `Init Done`，急停 `o` 干净自退 rc0。

## 已知约定

- 端口：GUI 8090；ZMQ 5556（streamer PUB）/ 5557（C++ 反馈）。
- streamer 在手柄 A+B+X+Y 急停后**主动退出是正常现象**。
- sim 与真机部署进程互斥，GUI 已强制。
