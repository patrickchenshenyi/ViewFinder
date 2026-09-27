#!/usr/bin/env python3
# M7.5: 跟踪状态日志 + 重初始化卡住兜底。
# 用法：python3 m7.5_patch_tum_mono_reset.py [/root/Photo-SLAM]
#
# 背景（2026-09-27 实测取证）：
#  - 实测发现 ORB-SLAM3（本 fork）本身已内置「丢跟踪恢复」：RECENTLY_LOST 持续 3s 重定位失败
#    → LOST → 自动建新地图（或 <10 KF 时 ResetActiveMap）→ 重新初始化。合成黑帧测试验证了
#    状态序列 3→4→0→1。因此无需再在应用层做「丢跟踪 N 秒后重置」——那是死代码。
#  - 真正可能的缺口：自动建新地图后的「重初始化长时间卡住」（NOT_INITIALIZED>30s 且曾 OK 过）。
#    本补丁在此情形下 ResetActiveMap 兜底重试。
#  - 同时给 tum_mono 加「跟踪状态变化日志」（[M7.5] 行），用于真机诊断死区原因
#    （OK-退化 vs 丢跟踪 vs 未初始化）。
#  - 附带测试钩子：环境变量 TUM_PACE_MS>0 时每帧 usleep，供本地回放/合成序列验证
#    丢跟踪路径时同步输入与跟踪线程（实时推流默认关闭）。
import sys, os

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/root/Photo-SLAM"
TUM = os.path.join(ROOT, "examples", "tum_mono.cpp")

def patch(path, pairs):
    s = open(path).read()
    for old, new in pairs:
        c = s.count(old)
        assert c == 1, "%s: pattern count=%d for:\n%s" % (path, c, old[:150])
        s = s.replace(old, new)
    open(path, "w").write(s)
    print("patched %s OK" % path)

patch(TUM, [
    # 1) 声明变量
    (
        "    // Main loop (incremental)\n"
        "    cv::Mat im;\n"
        "    int ni = 0;\n"
        "    while (true)\n",
        "    // Main loop (incremental)\n"
        "    cv::Mat im;\n"
        "    int ni = 0;\n"
        "    while (true)\n",
    ),
    # 2) TrackMonocular 后插入状态日志 + 兜底（含测试钩子）
    (
        "            // Pass the image to the SLAM system\n"
        "            pSLAM->TrackMonocular(im, tframe, std::vector<ORB_SLAM3::IMU::Point>(), vstrImageFilenamesRGB[ni]);\n",
        "            // Pass the image to the SLAM system\n"
        "            pSLAM->TrackMonocular(im, tframe, std::vector<ORB_SLAM3::IMU::Point>(), vstrImageFilenamesRGB[ni]);\n"
        "\n"
        "            // 测试钩子：TUM_PACE_MS>0 时每帧 usleep，让输入与实时跟踪线程同步\n"
        "            // （本地回放/合成序列验证丢跟踪路径用；实时推流下默认关闭）\n"
        "            {\n"
        "                static int paceMs = -1;\n"
        "                if (paceMs < 0) {\n"
        "                    const char* env = std::getenv(\"TUM_PACE_MS\");\n"
        "                    paceMs = (env && atoi(env) > 0) ? atoi(env) : 0;\n"
        "                }\n"
        "                if (paceMs > 0) usleep(paceMs * 1000);\n"
        "            }\n"
        "\n"
        "            // M7.5: 跟踪状态日志 + 重初始化卡住兜底。\n"
        "            // 注：ORB-SLAM3 本身在「丢跟踪 3s 重定位失败」后会自动建新地图重新初始化\n"
        "            //（RECENTLY_LOST→LOST→新地图），无需在这里过早重置；\n"
        "            // 这里兜底「重初始化长时间卡住」（NOT_INITIALIZED>30s 且曾 OK 过）。\n"
        "            {\n"
        "                static int lastLoggedState = -999;\n"
        "                static bool hasBeenOk = false;\n"
        "                static double notInitSince = -1.0;\n"
        "                int trackState = pSLAM->GetTrackingState();\n"
        "                if (trackState != lastLoggedState)\n"
        "                {\n"
        "                    std::cout << \"[M7.5] 状态变化 \" << lastLoggedState << \" -> \" << trackState\n"
        "                              << \" (t=\" << tframe << \"s, frame \" << ni + 1 << \")\"\n"
        "                              << \" [-1=未就绪 0=无图像 1=未初始化 2=OK 3=刚丢 4=丢失]\" << std::endl;\n"
        "                    lastLoggedState = trackState;\n"
        "                }\n"
        "                if (trackState == (int)ORB_SLAM3::Tracking::OK)\n"
        "                    hasBeenOk = true;\n"
        "                if (trackState == (int)ORB_SLAM3::Tracking::NOT_INITIALIZED && hasBeenOk)\n"
        "                {\n"
        "                    if (notInitSince < 0.0)\n"
        "                        notInitSince = tframe;\n"
        "                    else if (tframe - notInitSince > 30.0)\n"
        "                    {\n"
        "                        std::cout << \"[M7.5] 重初始化卡住 \" << (tframe - notInitSince)\n"
        "                                  << \"s，重置活动地图重试…\" << std::endl;\n"
        "                        pSLAM->ResetActiveMap();\n"
        "                        notInitSince = -1.0;\n"
        "                    }\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    notInitSince = -1.0;\n"
        "                }\n"
        "            }\n",
    ),
])

print("M7.5 PATCH DONE（需重编 bin/tum_mono）")
