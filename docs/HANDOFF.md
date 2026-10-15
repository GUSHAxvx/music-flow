# Music Flow 项目交接文档

> 用途：开启新对话时，把这份文档整份贴进去，即可获得完整上下文。
> 生成时间：2026-10-14 23:5x（本机时间）
> 阅读顺序建议：§1 架构 → §2 数据流 → §6 未完成的工作 → §4 Bug 全表

---

## 1. 项目是什么

**Music Flow**：本机（Windows）上的「音乐自动下载 → 整理 → 同步到手机」全自动流水线，带一个 Web 控制台（http://127.0.0.1:8477）。

它不是一个独立程序，而是**编排器**：把若干开源项目和自写脚本串成一条 10 步流水线，并提供一个 GUI 看进度、改设置、手动导入。

### 1.1 磁盘布局

| 路径 | 角色 |
|---|---|
| `D:\music-flow\` | **本项目根目录**。`music_flow.py`（约 2060 行）是唯一入口：HTTP 服务 + 流水线编排 + 步骤函数全在一个文件里 |
| `D:\music-flow\music_flow.py` | 主编排器。含 `PIPELINE` 步骤表、`s_*` 步骤函数、`Runner`（带停止/续跑）、HTTP handler、SSE |
| `D:\music-flow\import_songs.py` | 手动导入逻辑（文件/文件夹两种模式），被 `/api/import` 调用 |
| `D:\music-flow\config.json` | 唯一配置源（结构见 §5） |
| `D:\music-flow\config.example.json` | 配置模板（**需要补 `.kgma/.kgtemp` 说明**，见 §6） |
| `D:\music-flow\run_once.py` | 命令行一次性跑（不进 GUI） |
| `D:\music-flow\test_all_steps.py` | 端到端测试脚本 |
| `D:\music-flow\启动音乐管家.bat` | 用户日常启动方式：`cd /d %~dp0` + `python music_flow.py` |
| `D:\music-flow\static\` | 前端三件套：`index.html` / `app.js` / `style.css`（原生 JS，无框架） |
| `D:\music-flow\logs\` | 每次运行一个日志：`YYYYMMDD-HHMMSS_<模式>.log` |
| `D:\music-flow\scripts\` | 本项目的脚本：`fix_covers.py`(1576 行)、`kugou_kgm.py`(酷狗解密)、`audio_guard.py`、图标相关 |
| `D:\music-flow\docs\` | 文档目录（本文件所在处） |
| `D:\go-music-dl\downloads\` | **曲库本体**，也是 Syncthing 的 sendonly 文件夹。约 272 个文件 / 11.7 GB |
| `D:\music-monitor\` | 上游开源项目的落地脚本：`library-manager.ps1`(390 行)、`tag_files.py`(354 行)、`check_integrity.py`、`samplerate_guard_host.py`、`audit_quality.ps1` |
| `D:\music-decode\build\music_decode.exe` | 第三方解密器（Go），作为酷狗解密的**兜底**手段 |
| `C:\ProgramData\Syncthing\` | Syncthing 的 config/ 与 data/（`data\syncthing.log` 是它的日志，**不是**本项目的日志） |
| `E:\音乐\` | 用户手动导入时的源素材目录；`E:\音乐\KugouMusic\` 是酷狗客户端下载目录 |

### 1.2 三个外部服务

| 服务 | 端口 | 作用 |
|---|---|---|
| 本机 `music_flow.py` | 8477 | Web 控制台 + 流水线执行器 |
| Docker 容器 `go-music-dl` | — | 下载引擎（按收藏抓歌） |
| Docker 容器 `music-monitor` | — | 曲库管理（容器内跑 `/app/manage_library.py`） |
| 本机 Syncthing | 8384 (GUI/API) / 22000 (传输) | 把曲库同步到手机 |

配置里 `sync.api_key` 是 Syncthing API Key，`sync.phone_device` 是手机设备 ID。

---

## 2. 流水线（唯一真相：`music_flow.py` 的 `PIPELINE`）

两种模式共用这一张表：
- **省心模式（auto）**：从头跑到尾，任一步失败即停
- **单步（manual）**：只跑指定 step id

| # | id | 名称 | 干的事 |
|---|---|---|---|
| ① | `docker` | 启动 Docker | 拉起 Docker Desktop 并等引擎就绪 |
| ② | `containers` | 启动容器 | 启动 `go-music-dl` / `music-monitor` |
| ③ | `syncthing` | 启动 Syncthing | 拉起同步服务并等 GUI 就绪 |
| ④ | `download` | 检查并下载新收藏 | 按音质规则抓缺失曲目 |
| ⑤ | `decode` | 解码加密音乐 | `ncm/qmc/kgm` 还原成普通 flac/mp3 |
| ⑥ | `maintain` | 落盘与整理 | 取暂存→闸门→落盘→去重→写标签 |
| ⑦ | `covers` | 封面与歌词 | 给缺封面的补齐专辑图，顺带补歌词 |
| ⑧ | `verify` | 安全检查 | 拦截会炸耳的采样率，清理暂存 |
| ⑨ | `integrity` | 曲库体检 | 检查文件是否损坏、标签是否完整 |
| ⑩ | `sync` | 同步到手机 | 触发扫描并盯着传到完成（`"soft": True`） |

**顺序上的关键约束（有原因，不要随意调换）：**

1. **⑤ decode 必须在 ⑥ maintain 之前**。加密文件 `ffprobe` 读不出规格，会被 ⑥ 的采样率闸门当坏文件丢掉，手机也不认。
2. **写标签必须在落盘之后**。落盘会用暂存里的文件覆盖本地文件，暂存文件没有标签（酷狗返回的音频被 ffmpeg 重封装过，元数据丢失），先写会被覆盖掉。所以 `library-manager.ps1` 的「阶段 5/5 补写标签」放在最后。
3. **⑧ verify 用 `samplerate_guard_host.py`（宿主机版）**，不是容器版。

### 2.1 手动导入也走同一条收尾链

`import_songs.py` → `/api/import`（POST，`mode` = `files` | `folder`）。导入后会自动跑「导入后收尾」：⑤ 解码加密音乐 → ⑦ 封面与歌词 → ⑧ 安全检查 → 触发 Syncthing 扫描。

**用户明确要求过：手动导入也必须跑解密那一环。** 这是导入一次就完整的保证。

---

## 3. HTTP 接口（`music_flow.py` 里的 handler）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/pipeline` | 返回 PIPELINE 的 id/name/desc |
| GET | `/api/status` | 总状态：runner、docker、syncthing（`completion` / `need_items` / `need_mb` / `phone_connected`）、曲库统计 |
| GET | `/api/config` | 读配置 |
| GET | `/api/runs` | **运行记录列表**（`{name, size, mtime}`），读 `logs/*.log` |
| GET | `/api/run?name=<日志名>` | **读某次运行的完整日志内容** |
| GET | `/api/blocked` | 被拦截的曲目 |
| GET | `/api/library_stats` | 曲库统计（`?force=1` 强制重算） |
| GET | `/api/logic` | 「运行逻辑说明」文案 |
| GET | `/api/events` | SSE 实时推送 |
| POST | `/api/run` | 跑流水线：`{mode:"auto"}` 或 `{mode:"manual", step:"<id>"}` |
| POST | `/api/stop` | 停止当前运行（会终止整棵进程树） |
| POST | `/api/import` | 手动导入：`{mode:"files"|"folder"}` |
| POST | `/api/config` | 保存配置 |
| POST | `/api/logic` | 保存「运行逻辑说明」 |

### ⚠️ 排查问题时的正确姿势（用户为此批评过一次）

**不要 `cat`/导出日志文件，也不要去翻 Syncthing 的日志。** 用本项目自己的接口：

```
GET http://127.0.0.1:8477/api/runs                  # 列出所有运行
GET http://127.0.0.1:8477/api/run?name=<日志名>      # 读某次运行的日志
GET http://127.0.0.1:8477/api/status                # 当前状态与同步进度
```

有 `_srv.log` / `_srv.err` 这类临时文件是过去调试留下的垃圾，不要依赖它们。

---

## 4. Bug 全表（用户问过的问题 → 根因 → 解决方案）

### 4.1 加密音乐解密（酷狗 .kgma / .kgm / .kgg / .kgtemp）

**用户问**：「能解密这个格式的了吧」/「这有一个 kgma 文件的」/「这个解码器不只能解 kgg 格式的，你把酷狗下载的所有加密格式都设置进去，例如我刚才导入 kgma 时就失败了」

| # | 症状 | 根因 | 解决 |
|---|---|---|---|
| B1 | 导入 kgma 直接失败 | `DECODE_EXT` 里没有 `.kgma`（只有 `.kgm/.kgg/.vpr`），文件根本没被识别成加密音乐 | 扩到 `(".ncm",".qmc0",".qmc3",".qmcflac",".qmcogg",".kgm",".kgma",".vpr",".kgg")` |
| B2 | 认出来了但解不出 | 第三方 `music_decode.exe` 不支持 kgma | 自己用纯 Python 实现（见下） |
| B3 | 自己实现后开头是 `fLaC` 但内容是垃圾 | 酷狗 KGM 的头部常量**有两套**。网上流传的是 `7C D5 32 EB 86 02 7F 4B A8 AF A6 8E 0F FF 99 14`(16 字节)，但**磁盘上的真文件用的是 12 字节版**：`7C D5 32 EB 86 02 7F 4B A8 AF A6 8E` | 用 12 字节版；同时兼容 16 字节版 |
| B4 | 密钥流算不对 | **加密是数据相关的**，纯密钥流在数学上不成立 | 见 §4.2 最终算法 |
| B5 | `bytes.translate` 报 `ValueError: translation table must be 256 characters long` | `translate` 只接受正好 256 字节的表，且语义是 `table[输入字节]` —— 位置相关的变换塞不进一张定长表 | 放弃 translate，改「逐块 4096 平铺表 + 直接下标」 |
| B6 | 解出来「差不多个」但字节不对（`0x46` vs `0x66`） | 掩码是**按位置**而不是按字节；且 `blk = out[p0:e]` 是 **bytearray 切片 = 拷贝**，改它不会写回原数组 | 掩码按位置；切片结果必须显式写回 |
| B7 | ffmpeg 报「解码失败」 | **ffmpeg 的输出选项写在 `-i` 之前会被当成输入选项**，`-t 2 -i file` 产出 0 字节 | 正确写法：`ffmpeg -v error -i file -t 2 -f null NUL`（输出选项放 `-i` 之后） |

**最终算法（`D:\music-flow\scripts\kugou_kgm.py`）**，用「可证明的分解 + 逐块表」：

```
mask(p) = MASK_V2_PRE_DEF[p % 272] ^ _prefix_for_offset(p >> 4)
```

`p >> 4` 是块号，16 字节块内基本不变。核心是 `_apply_mask_range` + `MASK_V2_PRE_DEF`（272 项）+ `_prefix_for_offset`，逐 4096 字节分块处理。

**验证结果（至今可复现的判据）**：

- 输入 `D:\music-flow\_kgtest\real\in\real.kgma`（真文件，25,835,282 B）
- 参考基准 `D:\music-flow\_kgtest\verify\same_song_kgma.flac`（25,834,258 B）
- **逐字节相同：True**；前 8 字节 `66 4c 61 43 00 00 00 22`（`fLaC` + STREAMINFO 块头）
- 扩展名判定 `.flac`；耗时 **5.51 s / 25.8 MB**

**接入方式（`music_flow.py` 的 `s_decode`）**：先试内置模块 `_decode_via_python(src, tmpdir)`（读前 64 字节给 `can_decrypt` → 调 `mod.decrypt` → 产物过 `_verify_audio`），成功记日志「内置酷狗解密成功（.kgma/.kgm/.kgtemp 走这条路）」；失败才走 `_attempt(src)`，再不行才 `_decode_via_music_decode` 兜底。

**音频电平判据（判断解密正确性的第二道关）**：`volumedetect` 的 mean_volume 约 `-10.8 dB`（正常音乐）而不是约 `0 dB`（噪声）。

### 4.2 同步「每次重头下载」——**本次会话最重要的发现**

**用户问**：「不是，怎么重头开始下载？」/「你是不是又没有把之前已经下载的固定，不重新下载，怎么每次都是这样」

**根因：曲库里的文件被流水线无条件改写，Syncthing 于是把它们整个重传。**

曲库是 Syncthing 的 **sendonly** 文件夹。**只要文件被写一次，哪怕标签值和原来一模一样，Syncthing 就会重新算哈希、判定为新版本，然后把整个文件重传给手机。** 一首 Hi-Res 40–160 MB，几十首就是几个 GB。

实测证据（2026-10-14 23:5x）：

- `D:\go-music-dl\downloads` 里 **近 1 小时有 122 个文件被改写，共 5740.5 MB** —— 正好等于手机端 `needBytes` 的规模
- 手机完成度从 **99.84% 掉到 49.11%**，然后重新爬升，速率 5–9 MB/s
- 触发时机吻合：`23:33:52` 手机端强制断开（日志 `An existing connection was forcibly closed by the remote host`），`23:36:36` 重连后完成度已经掉到 49%

**两个改写源头（都已修）：**

#### 源头 1：`D:\music-monitor\tag_files.py`

`D:\music-monitor\library-manager.ps1` 第 352–381 行，每轮都无条件跑两遍：

```powershell
$tagOut = & python $tagScript --apply 2>&1 | Out-String      # 幂等：已有完整标签的不动
...
$forceOut = & python $tagScript --force --apply --only $forceList 2>&1 | Out-String
```

第二行是**强制覆盖 11 个「已知酷狗标签错乱」曲目**，`--force` 会绕过幂等检查、每次都重写这 11 个文件（含 114.6 MB 的 `Darren Hayes - Creepin' up on You.flac`、159.6 MB 的 `Daoko、米津玄師 - 打上花火.flac`）。

**修法**：新增 `render_tags(path, artist, title, out_path)` 与 `apply_tags(path, artist, title, unchanged_out=None)` —— 先把结果渲染到临时副本，再和原文件**逐字节比对**，一致就丢弃副本、原文件一个字节都不动（mtime 不变，Syncthing 不会重传）；只有真不同才 `os.replace` 换回去。`write_tags()` 改为直接调用 `apply_tags()`（旧调用兼容），原实现保留为 `_write_tags_unconditional()`。

**踩坑记录（两次失败）：**

1. 渲染到 `io.BytesIO()` 不行 —— mutagen 的 `save()` 会**回头去读原文件头**（`flac.py:863 __check_header`），内存缓冲区没有 `.name`，报 `error file said 4 bytes, read 0 bytes`。
2. 临时文件用空文件 + 自定义后缀 `.part` 也不行 —— mutagen 的 FLAC 写入器要求目标是一个**真实的副本**；MP3/OggVorbis/MP4 还会按**扩展名**判断「能否只写标签不重写音频」，扩展名不认识时直接拒绝写入。
   **正确做法：`shutil.copyfile(path, tmp)` 整份复制，临时文件保留原扩展名。**

**验证**：

```
第 1 次 --apply → 补写完成: 成功 2, 失败 0        （内容确实优化了，真写）
第 2 次 --apply → 内容未变跳过: 2（未写盘，不会触发同步重传）
mtime 前: 2026-10-14 23:53:06  后: 2026-10-14 23:53:06   （完全没动）
```

#### 源头 2：`D:\music-flow\scripts\fix_covers.py`

`write_into()`（1241 行）原本**无条件** `os.replace(tmp, path)` —— 哪怕封面/歌词已经有了、STATE 标记也没变，照样替换文件、改 mtime。`s_covers` 每轮 `--max-per-run 60`，意味着**每轮最多 60 个文件被无谓改写**。

**修法**：`write_into()` 改为返回 `bool`，改完副本先和原文件逐字节比，一致就 `os.unlink(tmp)` 直接返回 `False`，原件 mtime 与 inode 都不动。新增计数 `unchanged_pic` / `unchanged_lrc`，汇总时打印「内容未变跳过: 封面 N 首, 歌词 M 首（未写盘，不会触发同步重传）」。

**验证**（隔离测试，同一个文件跑两次）：

```
第 1 次 → ✓ 封面 136 KB / ✓ 歌词 794 字符      封面: 成功 1, 歌词: 成功 1
第 2 次 → 已有封面: 2, 缺封面: 0                封面: 成功 0, 歌词: 成功 0
mtime: 23:55:00.053826900  两次完全一致（没被碰）
```

#### 附带新增的可观测性：`sync_pending_report()`

用户明确批评过「检查问题时完全不会用你自己写的运行日志，每次都导出找问题」。所以给 `s_sync` 加了**自报家门**：在触发扫描之后、盯进度之前，直接打印「近 1 小时有多少文件被改写、共多少 MB、分别是哪些」。

实测输出：

```
[warn] 近 1 小时有 122 个文件被改写，共 5740.5 MB（这些会被同步重传）
[info]     23:54:54    44.3 MB  HANNIE、HICARI、Isaiah Dreads、Embody - Drama.flac
...
[info]     … 另 114 个
```

以后「重传」要么有据可查，要么就是真有问题，不用再翻 Syncthing 日志。

#### 还没做的（重要，留给新对话）

**已经躺在手机待传队列里的 GB 级数据没法撤回** —— 只能等它传完。这两个 fix 的作用是**让下一轮不再产生新的重传**。所以：

> 用户在下一轮跑之前，最好先看 `/api/status` 的完成度是否回到 100%，再跑流水线；跑完后看 `sync_pending_report` 的输出，如果还是打印「近 1 小时有 N 个文件被改写」，说明**还有第三个改写源头没找到**。

### 4.3 Syncthing 同步相关的其他事实

- Syncthing 本机版本 **v2.1.6**，手机 **v2.0.11**
- 文件夹 id：`go-music-dl-downloads`，类型 **sendonly**，path 是 `D:\go-music-dl\downloads`
- **sendonly 意味着：手机不在线时电脑推不了，只能干等。** 所以「卡在 96%~99% 很久」的真相是「一次 GB 级批次正常传完后，尾巴上某个文件没赶上手机在线」
- 传速实测 **33–36 MB/s**（局域网，2026-10-14 22:05 那轮：5455.8 MB / 120 项，4.5 分钟）；慢的时候 5–9 MB/s
- 手机完成度可用 `/rest/db/completion?device=<phone>&folder=<fid>` 查；`needItems` / `needBytes` 是权威数字
- `s_sync` 的既有设计：先花最多 90 秒判断手机可达性，连不上就**立刻明确报错并结束**（而不是无脑等 20 分钟）；连上后最多盯 20 分钟
- Syncthing 本机进程是 **一对**：`syncthing.exe` 父进程 + 子进程（真正持有 8384/22000 端口的是子进程）。启动瞬间会出现几次 `Failed to start API ... bind: 权限不足`，那是父进程先抢跑、随即退出，属正常现象，不是故障
- 本机 Syncthing 日志在 `C:\ProgramData\Syncthing\data\syncthing.log`（**排查同步问题不要优先读它**，见 §3 的接口）

### 4.4 其他已修的 bug（较早的会话）

| 症状 | 根因 | 解决 |
|---|---|---|
| 给 `.mp3` 文件写 ID3 头把文件写坏 | 酷狗大量文件是**伪装成 .mp3 的 FLAC**（文件头 `fLaC`、后缀 `.mp3`）。早期按扩展名选了 ID3 写入器 | **一律按文件头判断容器格式**，绝不按扩展名。`open_audio()` 统一处理 |
| 明明有标签的文件被误判「读取失败」 | `mutagen.File()` 按后缀挑解析器，后缀是 `.mp3` 但内容是 FLAC 时抛 `HeaderNotFoundError` | 同上，按 magic bytes 分派 |
| 176.4 kHz 的曲目在手机上放出来是噪声（实测炸耳） | 采样率超出手机链路能力 | `guard.block_sample_rates: [176400]`，由 `samplerate_guard_host.py` 拦截，不进同步文件夹 |
| 日志里报错信息是 `鍋囧姞瀵?flac` 这种乱码 | Windows 控制台编码是 cp936 | 统一 `PYTHONIOENCODING=utf-8`，日志按 cp936 解码回来 |
| 下载引擎走 POST 落盘时不嵌封面 | 引擎行为 | 由 ⑦ covers 在本机补 |

---

## 5. 配置（`D:\music-flow\config.json`）逐段说明

```jsonc
{
  "download": {
    "accept_hires": true,        // 接受 Hi-Res
    "accept_lossless": true,     // 接受无损
    "accept_lossy": false,       // 不接受有损
    "only_new_lossless": true,   // 无损只跟新歌，不回填历史
    "lossless_target": 30,
    "max_albums": 234,           // Hi-Res 目标数量
    "sort_mode": "newest",
    "min_bitrate": 0
  },
  "guard": {
    "block_sample_rates": [176400],   // 176.4kHz 会炸耳，拦掉
    "block_channels": [],
    "block_reason_176400": "176.4kHz 在本机手机链路上会输出噪声，实测炸耳",
    "content_mismatch_blocklist": true
  },
  "covers": {
    "enabled": true,
    "lyrics": true,
    "netease_lyrics": true,      // 中文歌词走网易云；会限流，靠冷却 + 连续 8 首 BUSY 收工兜住
    "max_per_run": 60            // 每轮最多处理 60 首
  },
  "decode": {
    "enabled": true,
    "threads": 0,
    "delete_source": true        // 校验产物能读才删原件
  },
  "sync": {
    "folder_id": "go-music-dl-downloads",
    "phone_device": "3WCYEVS-...",       // 手机设备 ID
    "api_key": "<Syncthing API Key>",
    "syncthing_gui": "http://127.0.0.1:8384",
    "syncthing_exe": "C:\\Program Files\\Syncthing\\syncthing.exe",
    "syncthing_args": ["--config=C:\\ProgramData\\Syncthing\\config",
                       "--data=C:\\ProgramData\\Syncthing\\data", "--no-browser"],
    "downloads_dir": "D:\\go-music-dl\\downloads"
  },
  "docker": {
    "desktop_exe": "C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe",
    "containers": ["go-music-dl", "music-monitor"]
  },
  "advanced": {
    "library_manager": "D:\\music-monitor\\library-manager.ps1",
    "manage_library_py": "/app/manage_library.py",
    "tag_files_py": "D:\\music-monitor\\tag_files.py",
    "check_integrity_py": "D:\\music-monitor\\check_integrity.py",
    "samplerate_guard_py": "D:\\music-monitor\\samplerate_guard_host.py",
    "audit_quality_ps1": "D:\\music-monitor\\audit_quality.ps1",
    "fix_covers_py": "D:\\music-flow\\scripts\\fix_covers.py",
    "music_decode_exe": "D:\\music-decode\\build\\music_decode.exe"
  }
}
```

**注意**：`downloads_dir` 在 `sync` 段里，不是 `paths` 段。（写代码时踩过 `cfg["paths"]["downloads"]` → `KeyError` 的坑。）

---

## 6. 未完成的工作 / 已知遗留

按优先级：

1. **验证第三改写源头是否存在**（§4.2 末尾）。跑一轮流水线，看 `sync_pending_report` 是否还报「近 1 小时 N 个文件被改写」。如果报，就是还有别的步骤在无条件写文件 —— 候选：`samplerate_guard_host.py`、`library-manager.ps1` 的「阶段 3 落盘」、`import_songs.py`。
2. **`config.example.json` 和 `README.md` 要补 `.kgma` / `.kgtemp` 支持说明**（`DECODE_EXT` 已支持 `.kgma`，但文档没跟上）。
3. **README 要说明内置酷狗解密**（`scripts/kugou_kgm.py`）和它依赖的 12 字节头常量。
4. **`.kgg` 借道方案已不需要**：内置模块现在能直接处理 kgma/kgm/kgtemp，`_decode_via_kgg` 这条 fallback 可以清理掉。
5. **`.kgtemp` 尚未实测**：`DECODE_EXT` 里有它，但手头没有真实样本。
6. **修 music-monitor 封面/歌词不统一的问题**（用户早前提过，尚未动手）。
7. **`D:\music-flow\` 根目录有 90+ 个 `_*.py` 调试脚本**（`_dbg*.py`、`_kg_*.py` 等）。它们是历史排查留下的，建议归档到 `_archive/` 或删掉，否则目录很难看。

---

## 7. 用户问过的问题清单（原文，便于对新对话核验）

按时间顺序：

1. 「现在在这个流程里，加入我之前提的那个文件转换的项目，但是不能乱来，要确保合理，并且不要忘了手动导入也要运行这个转换，所以你思考一下流程的优化」
2. 「所以你 4 块钱的量，花了 2 个小时就做了封面？如果你没有把这个加入未来同步的流程，或者导致同步又变得超久，我就要跳楼了」
3. 「能解密这个格式的了吧」
4. 「这有一个 kgma 文件的」
5. 「这个解码器不只能解 kgg 格式的，你把酷狗下载的所有加密格式都设置进去，例如我刚才导入 kgma 时就失败了」
6. 「简单看一下现在的同步情况，告诉我为什么这么慢」
7. 「所以现在什么情况，简述」
8. 「不是，怎么重头开始下载？」
9. 「你是不是又没有把之前已经下载的固定，不重新下载，怎么每次都是这样，而且我看你在检查问题时，也可能完全不会用你自己写的运行日志，每次都导出找问题，这样的情况，你设计的这个功能难道不是很失败吗？」
10. 「算了，你将当前项目的底层架构，修过的 bug，以及我问过的问题，你的解决方案，全部整合起来，仔仔细细的，我要开启新对话来修 bug 了，你现在每次想的都太久了」

### 用户的两条明确不满（新对话务必遵守）

- **不要重头下载**：已经传过的文件不该再传。改动曲库文件前先想「这会不会让 Syncthing 重传」。
- **用项目自己的日志接口排查，不要导出/翻外部日志**：`/api/runs` + `/api/run?name=` + `/api/status` 就是为此存在的。
- 附带一条：**不要想太久**，直接动手。

---

## 8. 常用操作速查

```bash
# 启动（用户日常方式）
cmd /c start "" "D:\music-flow\启动音乐管家.bat"

# 看状态
curl -s http://127.0.0.1:8477/api/status

# 列出所有运行记录
curl -s http://127.0.0.1:8477/api/runs

# 读某次运行日志
curl -s "http://127.0.0.1:8477/api/run?name=20261014-233938_省心模式全流程.log"

# 单步跑（阻塞式，不进 GUI）
python D:\music-flow\run_once.py --step sync

# 端到端测试
python D:\music-flow\test_all_steps.py

# 重启服务（旧代码不生效时）
taskkill /PID <pid> /T /F && cmd /c start "" "D:\music-flow\启动音乐管家.bat"
# 找 PID：netstat -ano | findstr :8477
```

### 改完代码一定要重启服务

`music_flow.py` 是常驻进程，**改完不重启不生效**。（本次会话就踩过：改完内置解密后，跑的还是旧代码 PID 27016。）

---

## 9. 值得保留的工程经验（省得再踩）

1. **`bytes.translate` 的表必须正好 256 字节**，语义是 `table[输入字节]`；位置相关的变换塞不进去。
2. **`bytearray` 切片是拷贝**，`blk = out[p0:e]` 改它不会写回原数组。
3. **mutagen 的 `save(路径)` 不是「另存为」**，而是「就地改写那个路径对应的文件对象」。要渲染副本，必须先 `shutil.copyfile` 出真实副本，且**保留原扩展名**。
4. **判断音频容器格式一律看文件头（magic bytes），绝不看扩展名。**
5. **ffmpeg 的输出选项必须写在 `-i` 之后**，否则被当成输入选项。
6. **Syncthing sendonly 文件夹里，写文件 = 触发整文件重传。** 任何「补标签/补封面」的步骤都必须幂等（内容没变就不写盘）。
7. 在 Windows 上启动独立进程用 `spawn_detached`，避免被「停止」按钮误杀整棵进程树。
