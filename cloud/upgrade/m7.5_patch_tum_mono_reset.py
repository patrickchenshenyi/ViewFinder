#!/usr/bin/env python3
# M7.5: 长时间丢跟踪自动重置 —— 连续丢跟踪超过阈值后 ResetActiveMap，
# 让推流后段也能被建模（而不是丢跟踪后一直空转浪费整段推流）。
# 用法：python3 m7.5_patch_tum_mono_reset.py [/root/Photo-SLAM]
# 说明：ResetActiveMap 清空当前地图（旧高斯已在 GaussianMapper 场景中，不受影响），
#       重新初始化得到新地图（独立坐标架）；若后续回环，ORB-SLAM3 会 merge 对齐。
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
    # 1) 声明丢跟踪计时变量
    (
        "    // Main loop (incremental)\n"
        "    cv::Mat im;\n"
        "    int ni = 0;\n"
        "    while (true)\n",
        "    // Main loop (incremental)\n"
        "    cv::Mat im;\n"
        "    int ni = 0;\n"
        "    double lostSinceT = -1.0;             // 连续丢跟踪的起始时间戳（<0 = 未丢）\n"
        "    const double kLostResetAfterS = 15.0; // 连续丢跟踪超过 15s → 重置活动地图\n"
        "    while (true)\n",
    ),
    # 2) TrackMonocular 后检测丢跟踪并重置
    (
        "            // Pass the image to the SLAM system\n"
        "            pSLAM->TrackMonocular(im, tframe, std::vector<ORB_SLAM3::IMU::Point>(), vstrImageFilenamesRGB[ni]);\n",
        "            // Pass the image to the SLAM system\n"
        "            pSLAM->TrackMonocular(im, tframe, std::vector<ORB_SLAM3::IMU::Point>(), vstrImageFilenamesRGB[ni]);\n"
        "\n"
        "            // M7.5: 长时间丢跟踪 → 重置活动地图重新初始化，让推流后段也能被建模。\n"
        "            // 旧地图的高斯已在 GaussianMapper 场景中（副本），不受 ResetActiveMap 影响；\n"
        "            // 新地图坐标架独立，若相机回到旧区域，ORB-SLAM3 回环会 merge 对齐。\n"
        "            {\n"
        "                int trackState = pSLAM->GetTrackingState();\n"
        "                if (trackState == (int)ORB_SLAM3::Tracking::LOST)\n"
        "                {\n"
        "                    if (lostSinceT < 0.0)\n"
        "                    {\n"
        "                        lostSinceT = tframe;\n"
        "                        std::cout << \"[M7.5] 跟踪丢失 (t=\" << tframe << \"s, frame \" << ni + 1\n"
        "                                  << \")，等待重定位，超过 \" << kLostResetAfterS << \"s 将重置…\" << std::endl;\n"
        "                    }\n"
        "                    else if (tframe - lostSinceT > kLostResetAfterS)\n"
        "                    {\n"
        "                        std::cout << \"[M7.5] 连续丢跟踪 \" << (tframe - lostSinceT)\n"
        "                                  << \"s，重置活动地图重新初始化…\" << std::endl;\n"
        "                        pSLAM->ResetActiveMap();\n"
        "                        lostSinceT = -1.0;\n"
        "                    }\n"
        "                }\n"
        "                else if (lostSinceT >= 0.0)\n"
        "                {\n"
        "                    std::cout << \"[M7.5] 跟踪恢复 (t=\" << tframe << \"s，共丢失 \"\n"
        "                              << (tframe - lostSinceT) << \"s)\" << std::endl;\n"
        "                    lostSinceT = -1.0;\n"
        "                }\n"
        "            }\n",
    ),
])

print("M7.5 PATCH DONE（需重编 bin/tum_mono）")
