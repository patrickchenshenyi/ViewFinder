# 用 CameraCharacteristics 取手机相机真实参数（fx/fy/cx/cy + 畸变）

> 目标：拿到 `photo_slam_config/phone_720p.yaml` 里那 9 个数（`fx, fy, cx, cy, k1, k2, p1, p2, k3`），
> 并且**知道它们对应的是哪一帧尺寸、哪个坐标系**。配套脚本：`scripts/android_intrinsics.py`。

---

## 0. 先明确边界：它给的是「静态声明」，不是「标定结果」

| 想要的东西 | CameraCharacteristics 能给吗 | 说明 |
|---|---|---|
| fx / fy / cx / cy | **可能给**，多数国产机给 null | `LENS_INTRINSIC_CALIBRATION`，OEM 出厂标定，填不填全看厂商 |
| k1 k2 p1 p2 k3 | **偶尔给** | `LENS_DISTORTION`（API 28+），同样常为 null |
| 焦距(mm)、传感器尺寸、像素阵列 | **一定给** | 有这些就能反推 fx/fy（见 §3），精度取决于厂商报的物理尺寸 |
| 变焦后的真实焦距/裁剪 | ❌ 静态参数给不了 | 必须读**每帧**的 `CaptureResult`（见 §6） |
| 录出来的 MP4 的真实内参 | ❌ | 还叠了 EIS、数字变焦、厂商裁剪、微信/系统转码 |

结论：**CameraCharacteristics 是很好的先验和交叉验证，但 SLAM/3DGS 的最终内参建议实拍标定一次**（§7）。
两者一致就放心用，不一致以标定为准。

---

## 1. 关键 Key 一览（照抄这张表就够）

| Key | API | 类型 | 含义 / 坑 |
|---|---|---|---|
| `LENS_INTRINSIC_CALIBRATION` | 23 | `float[5]` | `[fx, fy, cx, cy, s]`，**单位像素**，坐标系是**全像素阵列**（`SENSOR_INFO_PIXEL_ARRAY_SIZE`），**不是** active array，更不是你输出的 720p。`s` 是 skew（针孔模型不支持，一般 0）。可为 null |
| `LENS_DISTORTION` | 28 | `float[5]` | `[k1, k2, p1, p2, k3]`，Brown–Conrady，正好对应 ORB-SLAM3 的 5 个字段。可为 null |
| `LENS_RADIAL_DISTORTION` | 23（28 起废弃） | `float[?]` | 老字段、布局特殊，别用 |
| `LENS_INFO_AVAILABLE_FOCAL_LENGTHS` | 21 | `float[]` | 单位 mm。主摄通常 1 个值；多摄机型注意取哪一个 |
| `SENSOR_INFO_PHYSICAL_SIZE` | 21 | `SizeF` | **全像素阵列**的物理尺寸 mm（含非成像边缘像素） |
| `SENSOR_INFO_PIXEL_ARRAY_SIZE` | 21 | `Size` | 全像素阵列（像素） |
| `SENSOR_INFO_ACTIVE_ARRAY_SIZE` | 21 | `Rect` | 真正参与成像的区域（像素），坐标原点在**全像素阵列**左上角 |
| `SENSOR_ORIENTATION` | 21 | `int` | 输出图要**顺时针**转多少度才是设备自然方向的「正」。后摄常见 90 |
| `SCALER_STREAM_CONFIGURATION_MAP` | 21 | — | `getOutputSizes(ImageFormat.YUV_420_888)` 里找你的 720x1280 / 1280x720 |
| `SCALER_CROP_REGION`（逐帧） | 21 | `Rect` | **App 请求**的裁剪区域。IP Webcam 给的是全画幅 4096x3072 —— **别拿它算内参**，见 §6.2 |
| `android.scaler.rawCropRegion`（逐帧，QTI 私有） | — | `int[4]` | **硬件真正读出的**区域，本机 `[0 384 4096 2304]` = 16:9 居中裁剪。**算内参要用这个**，见 §6.2 |
| `android.scaler.croppingType`（静态，QTI 私有） | — | `byte` | 本机 `CENTER_ONLY` = 请求区域与输出宽高比不符时，HAL 居中裁剪而不是拉伸 |
| `DISTORTION_CORRECTION_AVAILABLE_MODES` | 28 | `int[]` | 含 `HIGH_QUALITY`/`FAST` 说明设备**会帮你把畸变矫直**，那你的画面里就没有 k1… 了 |
| `CONTROL_VIDEO_STABILIZATION_MODE` / `LENS_OPTICAL_STABILIZATION_MODE` | 21 | — | EIS/OIS 开启会额外裁剪+平移，内参会变 |
| `LOGICAL_MULTI_CAMERA_PHYSICAL_IDS` + `CameraManager.getPhysicalCameraIds(id)` | 28 | — | 逻辑多摄：主摄参数往往要取**物理**子相机的那一份 |
| `LENS_POSE_ROTATION` / `LENS_POSE_TRANSLATION` / `LENS_POSE_REFERENCE` | 23 | — | 多摄之间的外参，做多目/融合时才需要 |

---

## 2. 取数方式 A：手机 App 里打印（最可靠，参数最全）

```kotlin
// app/src/main/java/.../CameraProbe.kt   —— 最低要求 API 23；加 ?. 的都是「可能为 null」
import android.content.Context
import android.graphics.ImageFormat
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraCharacteristics.*
import android.hardware.camera2.CameraManager
import android.os.Build
import android.util.Log

private const val TAG = "CamProbe"

fun probeCameras(ctx: Context) {
    val mgr = ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    for (id in mgr.cameraIdList) {
        val c = mgr.getCameraCharacteristics(id)
        Log.i(TAG, "================ camera $id ================")
        if (Build.VERSION.SDK_INT >= 28) {
            Log.i(TAG, "physicalCameraIds = ${c.physicalCameraIds}")   // 多摄：主摄参数常要取物理子相机
        }
        // --- 出厂标定内参（像素；坐标系 = 全像素阵列）---
        val K = c.get(LENS_INTRINSIC_CALIBRATION)                     // API 23+
        Log.i(TAG, "intrinsicCalibration[fx fy cx cy s] = ${K?.joinToString() ?: "null"}")
        if (Build.VERSION.SDK_INT >= 28) {
            Log.i(TAG, "distortion[k1 k2 p1 p2 k3] = ${c.get(LENS_DISTORTION)?.joinToString() ?: "null"}")
        }
        // --- 推导内参所需的静态量 ---
        val f = c.get(LENS_INFO_AVAILABLE_FOCAL_LENGTHS)      // mm
        val phys = c.get(SENSOR_INFO_PHYSICAL_SIZE)           // mm，全像素阵列
        val pix = c.get(SENSOR_INFO_PIXEL_ARRAY_SIZE)         // px
        val act = c.get(SENSOR_INFO_ACTIVE_ARRAY_SIZE)        // Rect，原点在 pixel array
        Log.i(TAG, "focalLengths(mm)=${f?.joinToString()}  physicalSize(mm)=$phys")
        Log.i(TAG, "pixelArray=$pix  activeArray=$act  orientation=${c.get(SENSOR_ORIENTATION)}")
        if (f != null && phys != null && pix != null) {
            val pitchX = phys.width / pix.width                  // mm/px
            Log.i(TAG, "=> 推导 fx ${"%.2f".format(f[0] / pitchX)}  fy ${"%.2f".format(f[0] / (phys.height / pix.height))}")
        }
        // --- 畸变校正 / 防抖：决定「画面里还有没有畸变」---
        if (Build.VERSION.SDK_INT >= 28) {
            Log.i(TAG, "distortionCorrectionModes=${c.get(DISTORTION_CORRECTION_AVAILABLE_MODES)?.joinToString()}")
        }
        Log.i(TAG, "videoStabModes=${c.get(CONTROL_VIDEO_STABILIZATION_MODES)?.joinToString()}")
        // --- 你实际要用的输出尺寸是否支持 ---
        val map = c.get(SCALER_STREAM_CONFIGURATION_MAP)
        Log.i(TAG, "720x1280 支持? ${map?.getOutputSizes(ImageFormat.YUV_420_888)
            ?.filter { it.width == 720 && it.height == 1280 }}")
        if (Build.VERSION.SDK_INT >= 30) {
            Log.i(TAG, "zoomRatioRange=${c.get(CONTROL_ZOOM_RATIO_RANGE)}")
        }
        // --- 万能兜底：把这台机器所有能读到的 key 全打出来 ---
        c.keys.forEach { k -> Log.d(TAG, "  ${k.name} = ${runCatching { c.get(k) }.getOrNull()}") }
    }
}
```

跑一次，`adb logcat -s CamProbe` 就能看到全部静态参数；`c.keys` 那几行保证你不会「以为没有」。

## 3. 取数方式 B：不用写 App（adb dump）—— **本机已验证可用，优先走这条**

macOS 侧不需要 `brew install`（那要写系统目录），项目里已内置官方 platform-tools：

```bash
# adb 已解压在 tools/platform-tools/（签名 = Developer ID Application: Google LLC）
# scripts/adb.sh 会把 HOME 指到项目内 .adbhome/，避免 sandbox 写 ~/.android 被拒
bash scripts/adb.sh devices -l
bash scripts/adb.sh shell "getprop ro.product.model; getprop ro.build.version.release"

# 抓静态元数据（约 4MB / 7.7 万行）
bash scripts/adb.sh shell dumpsys media.camera > tools/_captures/camera_dump.txt
```

新版 Android（本机 OnePlus/ColorOS，Android 16）的 dump 里静态元数据长这样，
**值在 key 的下一行**，而且 5 颗相机挨着平铺：

```
== Camera HAL device device@1.1/vendor_qti/0 (v1.3) static information: ==
  API1 info:
    Facing: Back
    Orientation: 90
  API2 camera characteristics:
      android.lens.intrinsicCalibration (8000a): float[5]
        [2855.68188477 2855.21386719 2025.55200195 1526.74633789 0.00000000 ]
```

所以先列相机、再挑第几块（`--hal`），不要直接整文件解析：

```bash
python3 scripts/android_intrinsics.py --dump tools/_captures/camera_dump.txt --list-cameras
```

输出（2026-09-24 实测，一加 15 / PLK110）：

| HAL | Facing/Orient | 焦距 | 像素阵列 | intrinsicCalibration (fx, fy, cx, cy) | distortion |
|---|---|---|---|---|---|
| 0 | Back / 90 | 5.59mm | 4096x3072 | 2855.68, 2855.21, 2025.55, 1526.75 | 全 0 |
| 1 | Front / 270 | 3.23mm | 3280x2464 | 2018.75, 2018.75, 1640.00, 1232.00 | 全 0 |
| 2 | Back / 90 | 5.59mm | 4096x3072 | 2855.68, 2855.21, 2025.55, 1526.75 | 全 0 |
| 3 | Back / 90 | 2.31mm | 4096x3072 | 1905.19, 1904.92, 2026.17, 1579.48 | 全 0 |
| 4 | Back / 90 | 12.19mm | 4096x3072 | 9523.44, 9523.44, 2048.00, 1536.00 | 全 0 |

**这台机器 5 颗相机全都填了出厂标定**（`intrinsicCalibration` 不为 null），所以不需要走 §4 的推导。
`--list-cameras` 会顺带打印「由焦距推导的 fx」用于交叉验证：主摄 2795 vs 标定 2855.68，差 **2.1%**——
在厂商标称物理尺寸的正常误差内，**以出厂标定为准**。

> 副作用提醒：HAL 0 和 HAL 2 报的是同一组值（同一颗主摄的两种暴露），选 0 即可。
> 另外 `com.oplus.distortion.calibration`（`byte[3441]`）是 OPPO 私有的畸变标定 blob，无法直接解读，
> 别浪费时间；它至少说明厂商内部**是有**畸变标定的，只是不走 AOSP 标准字段。

---

## 4. 没有出厂标定时：从焦距 + 物理尺寸推导

```
像素间距 px_x = physicalSize.width  / pixelArray.width      # mm/px
           px_y = physicalSize.height / pixelArray.height
fx = focalLength_mm / px_x
fy = focalLength_mm / px_y
cx = pixelArray.width  / 2
cy = pixelArray.height / 2
```

- 用 `SENSOR_INFO_PHYSICAL_SIZE` 必须配 **`SENSOR_INFO_PIXEL_ARRAY_SIZE`**（不是 active array，也不是你的输出分辨率）——物理尺寸是按全阵列报的。
- 这只是「等效针孔」估计：厂商报的物理尺寸普遍有 ±2~5% 误差，且真实焦距会随对焦微调。
- 交叉验证：`EXIF` 里 `FocalLengthIn35mmFilm` 也有同样的信息量，`fx ≈ 输出宽 × f35 / 36mm`。

## 5. 换算到你的输出帧（最容易错的一步）

坐标系链条：

```
全像素阵列 pixelArray ──裁剪──▶ activeArray ──裁剪(SCALER_CROP_REGION)──▶ 缩放 ──▶ 输出帧
```

设目标帧 `W×H`，`(left, top, cw, ch)` 是**实际参与成像并缩放到输出帧**的那个矩形：

```
rotate 0/180 :   sx = W / cw ,  sy = H / ch
rotate 90/270:   sx = H / cw ,  sy = W / ch      # 旋转后传感器 x 轴落到输出 v 轴
u = (cx_px - left) * sx ,  v = (cy_px - top) * sy
fx' = fx * sx ,  fy' = fy * sy                   # 旋转 90/270 时 fx'/fy' 互换
```

三个必须记住的坑：

1. **旋转**：`SENSOR_ORIENTATION = 90` 的后摄录竖屏，图像是顺时针转 90° 得到的 → 内参要 `fx↔fy`、`cx↔cy` 相应互换。竖屏 720×1280 从 4:3 传感器出来，一定经过了「裁剪 + 旋转」两步，不是简单缩放。
2. **宽高比被改过就不能沿用小系数**：`sx ≠ sy` 说明画面被各向异性拉伸/裁剪。畸变系数只在**各向同性缩放**下可直接沿用（因为模型工作在 `x=(u-cx)/fx` 归一化坐标里）。`--crop` 传不进真实 crop region 时，宁可实拍标定。
3. **EIS / 数字变焦 / 多摄切换**会再改一次几何。录制时锁定 `CONTROL_ZOOM_RATIO = 1.0`、关掉 `CONTROL_VIDEO_STABILIZATION_MODE`，能让静态参数更接近实际。

> 本项目的具体情况：`sample_videos/seq` 的 720×1280 竖屏视频是**微信压成 2/3、无裁切**。
> 「均匀缩放 + 无裁切」意味着：**畸变系数原样保留**，而 `fx, fy, cx, cy` 只需按 `720 / 原宽` 这个**统一比例**缩放
> （在竖屏帧坐标系里缩放，不要拿传感器的横向宽度去算）。这正是脚本 `--out` 做的事。

## 6. 「某一帧的真实参数」要看 CaptureResult，不是 CameraCharacteristics

录制/取流时给 `setRepeatingRequest` 挂个 `CameraCaptureSession.CaptureCallback`，每帧读：

```kotlin
override fun onCaptureCompleted(session: CameraCaptureSession, req: CaptureRequest, r: TotalCaptureResult) {
    Log.d(TAG, "crop=${r.get(CaptureResult.SCALER_CROP_REGION)}")              // 本帧实际裁剪
    Log.d(TAG, "focalLength=${r.get(CaptureResult.LENS_FOCAL_LENGTH)}")        // 本帧实际焦距(mm)
    Log.d(TAG, "focusDistance=${r.get(CaptureResult.LENS_FOCUS_DISTANCE)}")    // 本帧对焦距离(屈光度)
    Log.d(TAG, "zoomRatio=${r.get(CaptureResult.CONTROL_ZOOM_RATIO)}")         // API 30+
    Log.d(TAG, "distortionMode=${r.get(CaptureResult.DISTORTION_CORRECTION_MODE)}")
}
```

这才是「真实」：静态参数是能力声明，`CaptureResult` 是这一帧实际发生的裁剪/焦距/校正模式。

### 6.1 但**不用写 App**：dumpsys 在推流时就会把逐帧结果打出来

手机连着 adb、App 正在推流时：

```bash
bash scripts/adb.sh shell dumpsys media.camera > tools/_captures/camera_dump_live.txt
# 找 "Device 0 is open. Client instance dump:" → "Latest received frame:" → 这里有逐帧 CaptureResult
```

### 6.2 ⚠️ 同一个 session 里有**两条 crop 字段，含义完全不同**（本项目最大的坑）

| 字段 | 本机实测值 | 含义 |
|---|---|---|
| `android.scaler.cropRegion` | `[0 0 4096 3072]` | **App 请求**的区域。IP Webcam 给的是全画幅 |
| `android.scaler.rawCropRegion` | `[0 384 4096 2304]` | **硬件真正读出**的区域（QTI vendor tag） |

**只看 `cropRegion` 会得出「4:3 被硬拉成 16:9，fx≠fy 差 33%」的错误结论。**
之所以不拉伸，是因为设备 `android.scaler.croppingType = CENTER_ONLY`：HAL 会把请求区域
**按输出宽高比居中裁剪**，裁完的真实区域在 `rawCropRegion` 里。

`rawCropRegion` 的字段约定厂商不统一（本机是 QTI 的 `x,y,w,h`，不是 Android 的 `L,T,R,B`）。
**别猜，用宽高比自证**：`(x,y,w,h)` → `4096x2304` = 16:9 且正好居中 ✅；
`(L,T,R,B)` → `4096x1920` = 2.13:1，没有任何流是这个比例 ❌。

### 6.3 一条命令验完（推荐在做任何标定之前先跑）

```bash
python3 scripts/verify_live_intrinsics.py --rtsp
```

它会：抓活体 dump → 报活跃 App 与三条流的尺寸 → 抓一帧 HTTP 图 + ffprobe 验 RTSP →
**用宽高比自证 `rawCropRegion` 的解读方式** → 换算内参并与 YAML 逐项对比（±0.5%）。
退出码 0=PASS，1=有人动了设置/换了机器/该更新 YAML 了。

2026-09-24 实测结果（一加 15 + IP Webcam 1280x720）：

```
活跃 App: com.pas.webcam      流: 1280x720(PRIVATE) / 1280x720(YUV) / 1920x1080(JPEG)
cropRegion=[0 0 4096 3072]    rawCropRegion=[0 384 4096 2304] → 4096x2304（16:9 居中）
zoomRatio=1.0  videoStabilizationMode=OFF  autoframingState=INACTIVE
activePhysicalId="2"（= 物理主摄，其标定与逻辑相机 0 一致）  lens.focalLength=5.59mm  ← 确认没切到超广/长焦
activePhysicalSensorCropRegion=[0 0 8192 6144]（物理传感器原生 50MP，逻辑相机 2×2 binning 成 4096x3072）
HTTP /shot.jpg = 1280x720     RTSP = h264 1280x720
→ fx=892.400 fy=892.253 cx=632.984 cy=357.109  与 YAML 差 0.000%  ✅ PASS
```

> 顺带解掉一个疑点：`LENS_INTRINSIC_CALIBRATION_MAXIMUM_RESOLUTION = [5590 5590 4096 3072 0]`
> 是 `focalLength/像素间距 = 5.59mm/1µm = 5590`、主点正好取阵列中心 —— 一看就是**名义推导值**（占位），
> 不是实测；而有实测痕迹的是 `LENS_INTRINSIC_CALIBRATION`（主点 2025.55/1526.75 明显偏心）。
> 两者差 2.1%，正是「标定值 vs 厂商标称物理尺寸」的正常误差 —— **以实测的那份为准**。

---

## 7. 强烈建议：用实拍棋盘格标定一次（10 分钟，一劳永逸）

它一次性把「裁剪 + 缩放 + 旋转 + 剩余畸变 + 厂商校正」全包进去，是唯一能直接对齐你那段 720×1280 视频的办法。
**这也是唯一能回答「IP Webcam 到底怎么裁」的办法**——静态参数只能给你一个假设，标定给你答案。

脚本：`scripts/calib_chessboard.py`（依赖装在 `tools/_venv/`，已含 opencv 5.0 + numpy）：

```bash
# 1) 打印/显示一张 9x6 格的棋盘（内角点 8x5），量出格边长（比如 25mm）
#    用【和实际建图完全一样的方式】拍：同分辨率、同方向、同 App（IP Webcam 就开 1280x720）
#    扫 20~30 个姿态，务必让棋盘出现在画面四角 + 中心 + 倾斜

# 2) 直接喂视频（脚本内部抽帧），或先 ffmpeg 抽成 png 再 --frames <目录>
tools/_venv/bin/python scripts/calib_chessboard.py \
    --video /path/to/calib.mp4 --every 5 --pattern 8x5 --square 25 \
    --expect 892.400,892.253,632.984,357.109

# 3) 认可结果后写回 YAML（只改数字，保留注释）
tools/_venv/bin/python scripts/calib_chessboard.py --frames /tmp/calib \
    --pattern 8x5 --square 25 --patch photo_slam_config/phone_1280x720.yaml
```

脚本内建的自检（**先跑这个，确认工具本身可信**）：

```
tools/_venv/bin/python scripts/calib_chessboard.py --selftest
→ 注入已知 K(fx=892.400, fy=892.253, cx=632.984, cy=357.109) + k1=-0.12 合成 16 张，
  标定回来 fx=892.312 / fy=892.243 / k1=-0.119，RMS 0.033px → PASS
```

判读：

| 现象 | 含义 / 对策 |
|---|---|
| RMS < 0.5px | 好；0.5~1.0 勉强；> 1.0 重拍（角点少 / 没覆盖四角 / 糊了） |
| `fx ≈ fy`（差 < 5%） | 各向同性缩放 ✅ → 静态值的 16:9 居中裁剪假设成立 |
| **`fx / fy ≈ 1.33`** | 画面被**各向异性**拉伸过 → 说明 IP Webcam 取的是 4:3 再硬拉成 16:9。对策：静态值改用 `--center-crop 4:3`（或直接不裁）重算 |
| 与 `--expect` 差 < 5% | 出厂标定与实拍互相印证，可放心用 |
| 与 `--expect` 差 > 15% | 裁剪/旋转/变焦假设错了，两个都先别信，先查 crop |
| \|k1\| > 0.5 | 畸变很大，ORB-SLAM3 的 PinHole 吃不下 → 先 `undistort` 把帧矫直再喂 |

手机镜头视场角大（等效 24mm 左右）时畸变明显，`calibrateCamera` 的 `k1` 通常在 `-0.2 ~ 0`。

---

## 8. 把结果写进配置

```bash
# ① 真机 dump（首选）。--center-crop 16:9 = 从 active array 居中裁出 16:9 再缩放，
#    等价于手工 --crop-ltwh 0,384,4096,2304（脚本会把你算出来的 crop 打出来）
python3 scripts/android_intrinsics.py --dump tools/_captures/camera_dump.txt --hal 0 \
    --center-crop 16:9 --out 1280,720 --rotate 0 --json

# ② 确认没问题后写回（只改数字、保留注释；先看它打印的 diff）
cp -n photo_slam_config/phone_1280x720.yaml photo_slam_config/phone_1280x720.yaml.bak
python3 scripts/android_intrinsics.py --dump tools/_captures/camera_dump.txt --hal 0 \
    --center-crop 16:9 --out 1280,720 --rotate 0 \
    --patch photo_slam_config/phone_1280x720.yaml

# 手抄了出厂标定（reference = 全像素阵列）
python3 scripts/android_intrinsics.py \
    --intrinsic 3040.5,3040.5,2016,1512 \
    --pixel-array 4032,3024 --active-array 0,0,4032,3024 \
    --distortion=-0.12,0.015,0.0008,-0.0004,0 \
    --out 720,1280 --rotate 90 --json
```

**`--rotate` 怎么定**（最容易反的一步）：

- 手机**竖屏**录 → 后摄 `SENSOR_ORIENTATION=90`，输出帧是传感器画面顺时针转 90° 的结果 → `--rotate 90`，`fx/fy` 会互换。
- 手机**横屏**录、且输出就是横的（本项目的 IP Webcam 1280x720）→ 传感器原生就是横向，
  流里没有旋转 → `--rotate 0`，只做居中裁剪 + 缩放。
- 判据：脚本会用 `--out` 的 W,H 去算 `fx/W`，落在 `0.6~1.8` 且 `fx≈fy` 才说明没搞反。

**已完成（2026-09-24，`photo_slam_config/phone_1280x720.yaml`）**：

```
HAL 0 主摄: fx=2855.6819 fy=2855.2139 cx=2025.5520 cy=1526.7463   （4096x3072 像素阵列）
→ 16:9 居中裁剪 (0,384,4096,2304) → scale=0.3125 → 1280x720：
   fx=892.400  fy=892.253  cx=632.984  cy=357.109   k1..k3=0
   水平 FOV 71.3°（机身自报 72.5°）  垂直 FOV 43.9°   fx/W=0.70
```

脚本会顺带做几项自检：`fx/W` 是否在合理区间、水平/垂直 FOV、`sx≠sy` 的各向异性警告、带畸变却 `fx≠fy` 的警告。
`--patch` 之前建议先复制一份 YAML（目录不是 git 仓库，改坏了没法 diff 回来）。

**验收标准**：

- `fx ≈ fy`，且 `fx / W` 在 `0.6 ~ 1.8`（等效 24~28mm 主摄大约在 1.0 上下）；
- 用 `2·atan(W / 2fx)` 反推的 FOV 与厂商标称（或 `f35` 换算）差在 5° 以内；
- 用标定出的 `fx` 和推出来的 `fx` 差 < 5%；
- 最后一步：拿这段视频在 ORB-SLAM3 里跑一遍，看 401 帧的轨迹漂移/尺度是否比 `fx=fy=720` 的粗值更好。

---

## 9. 常见问题

- **`--dump` 跑出来「一个字段都没识别到」**：两种原因，先分清。
  （a）这台机器的 dump 是**值在下一行**的格式（见 §3），而老解析器只认同行 `key: value`——
  已修，现在两种都吃；但仍然要用 `--hal N` 指明第几颗相机，否则默认取第 0 块。
  （b）**这台机器真的没填** `intrinsicCalibration`（很多机型如此）→ 走 §4 推导 + §7 标定。
- **算出来 `fx/fy ≈ 1.33`，怀疑画面被拉伸了？** 先别改系数——大概率是拿了 `cropRegion` 当裁剪区域
  （全画幅），而真正生效的是 `rawCropRegion`。见 §6.2，或直接跑 `scripts/verify_live_intrinsics.py`。
- **`adb devices` 报 `Cannot mkdir '~/.android': Operation not permitted`**：沙箱/权限不让写家目录。
  不要 sudo，也不要改系统权限——用 `bash scripts/adb.sh`（把 `HOME` 指到项目内 `.adbhome/`），或自己
  `HOME=$PWD/.adbhome adb ...`。注意 `ANDROID_USER_HOME` 在 platform-tools 35.0.2 上**不生效**。
- **`LENS_INTRINSIC_CALIBRATION` 一直是 null**：正常，很多机型不填。走 §4 推导 + §7 标定。
- **值和标定差很多**：优先信标定。出厂值可能是别的分辨率/别的物理子相机（多摄）下标的。
- **画面看着本来就没畸变**：说明设备默认开了畸变校正（`DISTORTION_CORRECTION_AVAILABLE_MODES` 里有 `HIGH_QUALITY`）。此时 k1…k3 应填 0，否则会「反向畸变」。到底校没校，用标定的 RMS 和直线测试判断。
  （一加 15 的 dump 里**没有** `distortionCorrection` 字段，`LENS_DISTORTION` 也全是 0——信息不足以判断，只能靠标定。）
- **竖屏内参一直不对**：先确认旋转方向。后摄 `SENSOR_ORIENTATION = 90` → 录出的竖屏图是顺时针转 90°；前摄通常是 270。
  验证办法：把镜头对准一个明显偏左的物体，看它在 720p 帧里出现在左边还是右边，据此判断参数是否用反。
- **30 秒卷尺验证（比读代码快）**：手机横屏固定，镜头正对一面墙，量出「画面左右边缘在墙上的距离」= `D`，
  再量镜头到墙的距离 = `L`。水平 FOV = `2·atan(D/2L)`。本项目预期 **71.3°**（`--out 1280,720` 那组数）。
  1m 距离时应看到约 **1.44m** 宽。差得离谱 → crop/rotate 搞错了，别去调 k1。
