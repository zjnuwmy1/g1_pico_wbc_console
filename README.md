# G1 PICO WBC Console

PICO 4 Ultra 全身遥操作 Unitree G1 的 step-by-step 向导 GUI，基于
[NVlabs/GR00T-WholeBodyControl](https://github.com/NVlabs/GR00T-WholeBodyControl) (GEAR-SONIC)。
支持 **MuJoCo 仿真**与**真机**遥操作，并在策略只控身体的基础上，额外打通了
**手**（仿真 BrainCo Revo2 overlay + 真机 BrainCo 手桥）和**真机模式切换**。

- 上游仓库（本目录只读取/启动它，**绝不修改**）：
  `~/gear_sonic_elbow_crawling_isaacsim/GR00T-WholeBodyControl`
- 零第三方依赖：系统 `python3` 直接跑（stdlib HTTP server + 原生 JS）。
  专属操作（xrt 探测 / 手桥 / 模式切换）在上游 `.venv_teleop` 子进程里跑。

## 启动

```bash
./start_console.sh          # → http://127.0.0.1:8090  (默认绑 0.0.0.0, 局域网可访问)
WBC_HOST=127.0.0.1 ./start_console.sh   # 只本机
```

或双击桌面 `G1-PicoWBC-Console.desktop`。

## 5 步流程

1. **环境检查** — 仓库/模型/二进制/venv/PC Service/网络 卡片，一键重检。
2. **安装缺失组件** — 一键 `install_pico.sh`（建 `.venv_teleop`）；PC Service 需 sudo，页面给出复制命令。
3. **PICO 连接** — 头显 XRoboToolkit app 填本机 IP（机器人网 `192.168.123.100`），分级探测 body data。
4. **Sim 遥操作** — 顺序启动 MuJoCo → deploy(sim) → PICO streamer；可切 **Dex3 / Revo2 手型**。
5. **真机遥操作** — 安全门 + **释放高层模式** → deploy(real) → streamer → **真机手桥**。

底栏实时显示 StreamMode / 步态 / CONTROL，并有红色 E-STOP。

## 真机额外能力（步骤 5）

### ① 机器人模式准备（`helpers/g1_mode.py`）
G1 开机默认在 **`ai` 高层模式**，会**无视外部 lowcmd** —— 直接起 deploy 机器人不会动。
按钮通过 `MotionSwitcherClient`（官方 `g1_low_level_example` 同款 `CheckMode → ReleaseMode` 循环）
释放高层控制，lowcmd 才能接管。释放 = 原生平衡脱离，**机器人变软靠吊架**，故带确认门、且强制先停 deploy。
遥操作结束「恢复高层模式 (ai)」。

> ⚠️ **每次重启机器人/deploy 都要重新释放模式 + 重启 streamer**（streamer 状态机跨会话残留，
> 否则 A+B+X+Y 会被当成急停把新 deploy 停掉）。

### ② 真机手控制（`helpers/brainco_hand_bridge.py`）
gear_sonic deploy 的手命令发 `rt/dex3`，但**真机 BrainCo Revo2 手听 `rt/brainco`** —— deploy 不控真机手。
手桥订阅 streamer 的 ZMQ pose（5556）读手柄模拟轴，映射成 6-DOF `MotorCmds_` 发 `rt/brainco/{l,r}/cmd`：

| 输入 | 控制 |
|---|---|
| **Trigger**（食指扳机） | 四指弯曲（模拟量） |
| **Grip**（中指侧握键） | 拇指整体闭合（metacarpal 跟随 ×0.8 + proximal） |
| **同侧摇杆 Y** | 拇指对掌微调（速率式，松手保持） |

**安全/过流保护**（位置伺服电流 ∝ 位置误差 → 钳误差即钳电流）：

- **失速钳制**：每指实测 q 落后命令 q 超过 `STALL_GAP` → 判定堵转，命令钳在 `实测+STALL_MARGIN`，
  不再硬推；物体拿走自动恢复跟随；张开方向永不钳。
- **slew 限速** `2.5/s`、`WBC_HAND_MAX_CLOSE` 上限。
- **staleness 自动张开**：>1s 无 pose（streamer 停 / 急停 / PLANNER 模式）→ 手温和自动张开，
  不会死握冻住（GUI 急停故意不杀手桥，靠此路径开手）。
- 退出 `os._exit(0)` 绕过 xrt 析构段错误。

环境变量：`WBC_HAND_MAX_CLOSE` / `WBC_THUMB_META_FOLLOW` / `WBC_THUMB_STICK[=0]` / `WBC_THUMB_STICK_INVERT` / `WBC_REVO2_RAWHAND`。

PC2 上需先跑 BrainCo 服务端：`~/brainco_hand_service/bin/brainco_hand_server`（`dialout` 组免 sudo），
仓库外脚本 `~/start_brainco.sh` 按 PID 杀旧实例 + setsid 起新（开机后左手第一次扫常失败，跑一两次即双手绑定）。

## 手动驾驶页（步骤 6，无需 PICO · `helpers/gui_drive.py`）

不戴头显，直接用 GUI 按钮驱动机器人。原理：**替代 streamer**，`gui_drive.py` 绑 ZMQ 5556 PUB，
import 上游 `build_command_message` / `build_planner_message`，把按钮意图发成 deploy 期待的
**PLANNER（行走规划）**消息 —— 只需 gait + 移动向量 + facing，不依赖身体追踪。

- 页面：①启动驾驶后端 ②启动/停止策略 + 步态下拉（Walk/Run/Squat/Kneel/Lying/Crawl/…）+ 速度滑条
  ③移动 D-pad（前后/左右移，**按住生效松开停**）+ 左转/右转；键盘 `W/S/A/D/Q/E`。
- `/api/drive` 把意图 JSON 写进 gui_drive 的 stdin；20Hz 持续发 planner。
- **与 PICO 数据流互斥**（都绑 5556，GUI 已强制）。真机用前同样要**释放高层模式**、deploy 在跑。
- 急停照旧走 deploy `o`（footer 红钮 / 终端 `o`）。

## 数据录制页（步骤 7，模仿学习 / StageAct · `helpers/episode_recorder.py`）

在 **PICO 全链路遥操作**跑起来后，录制同步的 **(图像 + 本体 + 手 + 动作)** 帧用于模仿学习。
采用 **"Option A"**：录的**动作 = 操作者的遥操作指令**（腕 3 点目标 + 扳机/握），训练出的策略
可直接回灌同一套 WBC + 手桥执行，不改执行栈。

订阅 4 路、30Hz 对齐快照、存原始字节（零额外依赖，免 cv2）：

| 类别 | 内容 | 来源 |
|---|---|---|
| 动作 | `vr_position`(腕3点位置) `vr_orientation`(wxyz) `left/right_trigger`+`grip` `*_hand_joints` | ZMQ 5556 `pose` 消息（自描述 header 通用解析） |
| 图像 | 头相机 RGB（JPEG）+ depth（PNG-16），原始字节存盘 | PC2 ZMQ 55555 / 55565 |
| 本体 obs | 真机 29 关节 q/dq + 躯干 IMU | DDS `rt/lowstate` |
| 手 obs | 手 6 关节 q | DDS `rt/brainco/{left,right}/state`（MotorStates_） |
| 触觉 obs | 每手 65 维（normal/tangential force + proximity + status，5 指） | DDS `rt/brainco/{left,right}/touch`（TouchHand_，见 `helpers/touch_idl.py`） |

- 落盘：`recordings/<session>/episode_XXXX/`（`frames/*.jpg`+`*.png` + `data.json`，每帧带 `action`/`state`/`stage`/时间戳）。
- 控制：GUI 按钮（`/api/record` → recorder stdin）**或** PICO 手柄数据采集键（pose 里 `toggle_data_collection` 边沿）起停，abort 键丢弃。
- **阶段标注**：拿瓶子分 接近→对位→抓取→提起，录制时点对应阶段按钮，边界写进每帧（StageAct 用）。
- 状态从日志 `[rec] STATUS` 行解析显示（rec/ep/帧数/stage/saved）。
- 训练前用离线脚本把 `data.json` + 原始帧特征化成 StageAct 张量（解耦录制与训练格式）。

## 仿真 Revo2 手 overlay（`overlay/`）

把 sim 里的 Unitree Dex3 三指手换成 **BrainCo Revo2 六指手**，用于手指 sim-to-real，
**不修改只读上游**（overlay 副本 + 启动器 monkeypatch）：

- `build_revo2_model.py` — ElementTree 手术，从上游 g1 身体 + 编译后的 brainco 手 MJCF 生成合并模型
  （`g1_revo2/`，被 .gitignore，clone 后跑此脚本重新生成）。要点：去 mimic distal 关节、补
  damping/armature(防抽搐)、手腕挂载 quat 从官方反解、修 actuator 顺序对齐关节树、关手碰撞(防自穿模)。
- `run_sim_loop_revo2.py` — monkeypatch `load_wbc_yaml` 指向 overlay yaml + 修 effort 裁剪 + 手映射
  （Stage-2：订阅 streamer 对称手信号驱动，绕开 deploy 不对称手通道），跑 gear_sonic sim。
- 步骤 4 的「手型」开关 = `hand_mode`（`dex3`/`revo2`），revo2 时 sim_loop 起 `run_sim_loop_revo2.py`。

## 安全设计（重要）

- **急停**：底栏红色 E-STOP / 手柄 A+B+X+Y / deploy 终端 `o` —— 向部署进程 stdin 写官方急停字符 `o`（策略干净自退），再 SIGINT streamer；手桥靠 staleness 自动张开。
- **真机部署进程绝不 SIGKILL**（本机有 arm_sdk 强杀后卡死、只能断电的历史）。优雅停止梯度：`o` → SIGINT → SIGTERM → 「需人工处理」横幅。
- deploy 的状态机是 `INIT → WAIT_FOR_CONTROL → CONTROL`。到 `Init Done` 进的是
  **`WAIT_FOR_CONTROL`(待命,安全 hold,机器人不动)**;**只有收到 5556 上的 `start` 信号
  （streamer 开始组合键 / gui_drive「启动策略」按钮 → `command{start:True}`）才进 `CONTROL`**,
  此时才缓动到默认站姿、开始发力。所以"deploy 起来了姿势没变"是**正常**的,不是故障。
  机器人真正开始动 = 你点了 start,安全门 + 姿态确认要卡在**点 start 前**。
- 步骤 5 安全确认**不持久化**，每次进真机模式都要重新确认。
- sim 与真机部署进程互斥，GUI 已强制。

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | HTTP API + 向导状态 + 安全 token + 手型/模式切换端点 |
| `procman.py` | 进程编排（分级停止策略 / 日志环形缓冲 / stdout 标记解析） |
| `checks.py` | 预检（逻辑复制自 repo 的 launch_pico_sonic_real.py，无运行时耦合） |
| `config.py` | 路径 / 端口 / stdout 标记正则 / DEPLOY_ENV（单一事实源） |
| `helpers/pico_probe.py` | 在 `.venv_teleop` 下探测 xrt body data |
| `helpers/g1_mode.py` | 真机 G1 高层模式 检查 / 释放(lowcmd 准备) / 恢复 |
| `helpers/brainco_hand_bridge.py` | 真机 BrainCo Revo2 手桥（含限速 + 失速过流保护 + stale 自动张开） |
| `overlay/build_revo2_model.py` | 生成 Revo2 手合并 MJCF（不碰上游） |
| `overlay/run_sim_loop_revo2.py` | 带 Revo2 手 overlay 的 sim 启动器 |
| `helpers/gui_drive.py` | 手动驾驶后端：GUI 按钮 → deploy planner（前后/转向/换步态），无需 PICO |
| `helpers/episode_recorder.py` | 数据录制后端：同步 (图像+本体+手+动作) 帧 → `recordings/`，模仿学习/StageAct |
| `static/` | 中文向导 UI |
| `logs/<时间戳>/<进程>.log` | 每次运行的完整进程日志（.gitignore） |

## deploy 二进制修复（已完成）

仓库自带的 `g1_deploy_onnx_ref` 启动即 `free(): invalid pointer` 崩溃。根因：编译时自动检测到
ROS jazzy（`HAS_ROS2=1`），运行时 jazzy 的 `libddsc` 抢在 unitree_sdk2 自带 `libddsc` 之前加载，
两版 CycloneDDS ABI 不兼容。**修法**：`HAS_ROS2=0` 重编（`just build`），只链 unitree libddsc。
重编/运行环境已固化进 `config.DEPLOY_ENV`（GUI 自动带上）：

```
HAS_ROS2=0
TensorRT_ROOT=~/.local/opt/tensorrt-10.13.3       # 必须 10.13.3，支持 RTX5080 Blackwell sm_120
onnxruntime_ROOT=~/.local/opt/onnxruntime-1.22.0
CUDA_HOME=CUDAToolkit_ROOT=~/.local/opt/cuda-runtime-12.6
MSGPACK_ROOT=~/.local/opt/msgpack-cxx/usr
```

运行加 `--output-type zmq`（HAS_ROS2=0 无 ROS2 通道）。

## 已知约定

- 端口：GUI 8090；ZMQ 5556（streamer PUB）/ 5557（C++ 反馈）。DDS domain 0，真机接口 `enp132s0`(192.168.123.x)，sim 用 `lo`。
- streamer 在手柄 A+B+X+Y 急停后主动退出是正常现象（SDK 析构必崩，无害）。
- sim 与真机部署进程互斥，GUI 已强制。
- 仅一个手桥实例可运行（两个发布者抢 `rt/brainco/*/cmd` 会互相打架）。

## 复现（clone 后）

```bash
# 1. 上游 GR00T-WholeBodyControl + .venv_teleop + 重编 deploy 二进制（见上游文档）
# 2. 生成 Revo2 手 overlay（被 .gitignore）
python overlay/build_revo2_model.py
# 3. 启动
./start_console.sh
```
