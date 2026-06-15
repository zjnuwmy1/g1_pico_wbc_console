# G1 × PICO 全身遥操作 — 工作记录 & 重启恢复指南

> 日期：2026-06-08。电脑重启后照「一分钟快速恢复」做即可。
> 控制台目录：`~/Desktop/g1_pico_wbc_console/`

---

## ⚡ 一分钟快速恢复(重启后照这个顺序)

重启后所有进程都没了,按顺序拉起来:

```bash
# 1) WiFi 重启后常被 rfkill 软关 → 先打开(头显走的 WiFi 挂在机器人网下)
nmcli radio wifi on
ip -br addr | grep -v lo          # 确认 enp132s0=192.168.123.100 在, wlp131s0 也 UP

# 2) 启动控制台 GUI(绑 0.0.0.0,本机和局域网都能开)
~/Desktop/g1_pico_wbc_console/start_console.sh
#   → 浏览器开 http://192.168.123.100:8090  (或本机 http://127.0.0.1:8090)
```

然后在 GUI 里:
- **步骤 1** 点「重新检查」,应全绿(PC Service 那项可能要去步骤 3 启动)
- **步骤 3** 点「启动 PC Service」(runService.sh 会 fork 守护进程,显示"已退出"是正常的,pill 会变「运行中 ✓」)
- **步骤 4** 依次启动:`① sim_loop` → `② deploy_sim`(等「控制中/就绪」)→ `③ pico_streamer`(勾可视化)
- 头显侧:连机器人网的 WiFi、app 里 PC IP 填 **`192.168.123.100`**、Tracking 勾 Head+Controller、Data/Control 选 **Send**、Motion Tracker 选 **Full body**;tracker 配对+校准
- GUI 步骤 3 点「探测 PICO 连接」→ 看到 **`全身就绪 ✅`** 就行
- 遥操作:摆校准姿势 → 手柄 **A+B+X+Y** 启动 → **A+X** 进 POSE 全身跟随

---

## 这套东西是什么

用 **PICO 4 Ultra(头显+2手柄+2脚踝tracker)** 通过 NVIDIA **GR00T-WholeBodyControl (GEAR-SONIC)** 全身遥操作 Unitree G1,先 MuJoCo 仿真。

- 上游仓库(只读取/启动,**绝不改**):`~/gear_sonic_elbow_crawling_isaacsim/GR00T-WholeBodyControl`
- 我做的向导 GUI(独立目录):`~/Desktop/g1_pico_wbc_console/`,零依赖、系统 python3、端口 **8090**

### GUI 文件
| 文件 | 作用 |
|---|---|
| `server.py` | HTTP API + 向导状态 + 安全门 + APK/PC Service 状态 |
| `procman.py` | 进程编排(分级停止、日志、stdout 标记解析、PYTHONUNBUFFERED 实时日志) |
| `checks.py` | 预检 + 网络 IP 探测(`lan_ips`/`headset_host`) |
| `config.py` | 路径/端口/标记正则/`DEPLOY_ENV`(单一事实源) |
| `helpers/pico_probe.py` | venv 下探测 xrt 全身数据(分级诊断) |
| `static/` | 前端向导 UI(中文) |
| `logs/<时间戳>/*.log` | 每进程完整日志 |

---

## 完整链路 & 启动顺序(sim)

3 个进程,GUI 步骤 4 里按顺序点:

1. `run_sim_loop.py` — MuJoCo 仿真(机器人在**这个窗口**里动,不是 PyVista 那个)
2. `deploy.sh --input-type zmq_manager --output-type zmq sim` — C++ 策略(等 `Init Done`)
3. `pico_manager_thread_server.py --manager --vis_vr3pt --vis_smpl` — PICO 数据流(PUB ZMQ 5556)

数据流：PICO头显→PC Service(60061)→streamer(PUB 5556)→deploy(SUB 5556)→DDS lowcmd→MuJoCo

---

## 已修复的关键问题(为什么现在能跑通)

### 1. deploy 二进制启动即崩 `free(): invalid pointer`(已重编修好)
- **根因**:二进制原来按 `HAS_ROS2=1` 编译,运行时 ROS jazzy 的 `libddsc` 抢在 unitree_sdk2 自带的之前加载,两版 CycloneDDS ABI 冲突 → 创建 DDS writer 时堆破坏。
- **修法**:用 `HAS_ROS2=0` 重新编译(`just build`),二进制只链 unitree libddsc。
- **重编环境(已固化进 `config.DEPLOY_ENV`,GUI 自动带上,重启后无需重编)**:
  ```
  HAS_ROS2=0
  TensorRT_ROOT=~/.local/opt/tensorrt-10.13.3      # 必须 10.13.3,支持 RTX5080 Blackwell sm_120
  onnxruntime_ROOT=~/.local/opt/onnxruntime-1.22.0
  CUDA_HOME=CUDAToolkit_ROOT=~/.local/opt/cuda-runtime-12.6
  MSGPACK_ROOT=~/.local/opt/msgpack-cxx/usr
  ```
  - TRT 必须用 **10.13.3**:10.2.0 不支持 5080(`Unsupported SM: 0xc00`),且其 `_win` cask 库会让引擎构建失败(`getNbCaskKLibs_INTERNAL_WIN`)。
  - 重编脚本留存：`/tmp/rebuild_deploy.sh`(重启后 /tmp 会清,但二进制和 .trt 引擎已生成、持久,**不用再编**)。
- sim 必须经 deploy.sh 跑(它对 sim 自动加 `--disable-crc-check`;裸跑会刷 3 万行 CRC Error 永不就绪)。

### 2. WiFi 被 rfkill 软关 → `nmcli radio wifi on`(重启后大概率要再来一次)

### 3. PC Service「启动了又退出」是正常的
`runService.sh` 只是启动器,fork 出 `RoboticsServiceProcess` 守护进程后自己 rc=0 退出。GUI 已改成查 60061 socket 显示真实状态(pill「运行中 ✓」)。

### 4. 网络拓扑(关键)
- 工作站:`enp132s0=192.168.123.100`(机器人有线网)、`wlp131s0`(WiFi)
- **头显的 WiFi 挂在机器人网(192.168.123.x)下**,所以头显 app 里 PC IP 填 **`192.168.123.100`** 能直连
- PC Service 头显端口:TCP `63901` + UDP `35065`(ufw 没挡机器人网段,实测头显 192.168.123.108 连上了)

### 5. GUI 改动汇总
- 全中文化(模式/步态中文括注英文)
- 绑 0.0.0.0(局域网可访问;`WBC_HOST=127.0.0.1 ./start_console.sh` 可改回只本机)
- 步骤 3 列出所有局域网 IP(WiFi/有线标注)
- pico_probe 分级诊断(SDK→PC Service→头显链路→Tracker→全身就绪)+ 退出 `os._exit(0)` 避免 SDK 析构崩溃
- streamer 退出码 -6 显示成中性「已停止」(SDK 析构必崩,无害)
- 所有子进程加 `PYTHONUNBUFFERED=1`(实时日志)

---

## 头显侧设置(PICO + XRoboToolkit app)

### PICO 系统:配对 + 校准 tracker
1. 设置 → 左侧最下 **Developer** → 关掉 **Safeguard**(看不到 Developer 就反复点 Software)
2. 点 **WiFi 图标** → 头显图片上方小圆圈 = Motion Tracker(或直接开 Motion Tracker app)
3. 每个 tracker 的「i」→ 全部 unpair → 右上「Pair」→ **长按 tracker 顶部按钮 6 秒**(红蓝闪)配对 2 个
4. 戴头显于眼睛 → 蓝色「Calibrate」→ ① 直立双手垂体侧 ② 低头看脚识别 tracker → 完成后头显推到额头

### XRoboToolkit app
- PC IP 填 **`192.168.123.100`**,Status 应 **WORKING**
- Tracking 勾 **Head + Controller**
- Data/Control 选 **Send**
- Pico Motion Tracker 选 **Full body** ← 全身关键

> tracker 要点:灯朝上、绑紧脚踝、裤腿收起别遮、充好电。

---

## 遥操作手柄手册

| 操作 | 按键 |
|---|---|
| 启动策略 / 急停 | **A+B+X+Y**(首次=启动+全身校准 CALIB_FULL;再按=急停 OFF) |
| 全身跟随 POSE ↔ 行走规划 | **A+X** |
| 上身冻结+行走 | **B+Y** |
| 三点跟随 VR_3PT(无tracker时用) | **左摇杆按下** |
| 走路 / 转向 | 左摇杆 / 右摇杆横向 |
| 切步态(走/跑/蹲/跪/爬…) | A+B 下一个 / X+Y 上一个 |
| 抓握 | 对应手扳机 |

**启动流程**:摆校准姿势(站直、双脚并拢、大臂垂体侧、小臂前屈90° L形、掌心相对)→ A+B+X+Y → 对齐手臂 → A+X 进 POSE。
**安全**:切 POSE/VR_3PT 前必须先对齐姿态;急停 = A+B+X+Y 或 GUI 红色按钮(向 deploy 发官方急停字符 `o`,干净自退)。

---

## 当前进度(重启前)

- ✅ GUI 全链路跑通:sim_loop + deploy_sim(Init Done) + pico_streamer + PICO 数据 fps~30
- ✅ 头显+手柄连上(192.168.123.100),探测 `全身就绪 ✅`(body_data: True)
- ✅ tracker 已穿戴+配对+校准,app 选了 Full body
- ⏭ **下一步**:摆校准姿势 → A+B+X+Y 启动 → **A+X 进 POSE 全身模式**,验证腰/腿是否跟随(之前 VR_3PT 上半身时腰"被钉住"是正常的,POSE 全身就会动)

---

## 已知坑 / 排错

- **探测里 `Tracker(0)/Tracker数据` 显示 0 但 `全身就绪 ✅`**:正常。Full body 模式下 tracker 数据融进 SMPL 主流,不走我探测的那个独立 motion_tracker 接口。看**最后一个灯 `全身就绪`** 为准。(待优化:body_data 为真时不再显示那两个灯像没通)
- **腰/身体"被钉住"**:上半身模式(VR_3PT)+ IDLE 步态的正常现象(平衡控制器扶直躯干、原地不动)。要全身动 → POSE 模式 + tracker;要走动 → 推左摇杆 / A+B 切步态。
- **streamer 退出 rc=-6 / "terminate called without an active exception"**:SDK 退出析构必崩,无害,GUI 显示「已停止」。
- **deploy 刷 CRC Error 永不就绪**:说明没走 deploy.sh(漏了 --disable-crc-check),用 GUI 启动即可。
- **机器人在哪个窗口动**:MuJoCo(run_sim_loop)窗口,不是 streamer 的 PyVista 可视化窗口。
- **GUI 显示"已退出"但其实在跑**:PC Service 是这样(查 60061);streamer 偶尔日志缓冲滞后(已加 PYTHONUNBUFFERED)。

---

## 相关 memory(已存)
- `project_g1_pico_wbc_console` — 本 GUI 总览
- `project_g1_gr00t_deploy_libddsc_abi_crash` — deploy libddsc ABI 崩溃根因 + 重编修复
