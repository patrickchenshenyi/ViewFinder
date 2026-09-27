#!/usr/bin/env python3
# M4.1: 把 tum_mono.cpp 从「一次性 LoadImages + 固定 for 循环」改成「轮询 rgb.txt 增量处理」
# 用法（在云端运行）：python3 m4.1_patch_tum_mono.py
import sys

p = "/root/Photo-SLAM/examples/tum_mono.cpp"
s = open(p).read()

# --- 替换 1：去掉开头的 LoadImages + nImages 空检查 ---
old1 = '''    // Retrieve paths to images
    std::vector<std::string> vstrImageFilenamesRGB;
    std::vector<double> vTimestamps;
    std::string strFile = std::string(argv[4]) + "/rgb.txt";
    LoadImages(strFile, vstrImageFilenamesRGB, vTimestamps);

    // Check consistency in the number of images and depthmaps
    int nImages = vstrImageFilenamesRGB.size();
    if (vstrImageFilenamesRGB.empty())
    {
        std::cerr << std::endl << "No images found in provided path." << std::endl;
        return 1;
    }
'''
new1 = '''    // Retrieve paths to images (incremental mode: poll rgb.txt continuously)
    std::vector<std::string> vstrImageFilenamesRGB;
    std::vector<double> vTimestamps;
    std::string strFile = std::string(argv[4]) + "/rgb.txt";
'''

# --- 替换 2：vTimesTrack + 主循环 ---
old2 = '''    // Vector for tracking time statistics
    std::vector<float> vTimesTrack;
    vTimesTrack.resize(nImages);

    std::cout << std::endl << "-------" << std::endl;
    std::cout << "Start processing sequence ..." << std::endl;
    std::cout << "Images in the sequence: " << nImages << std::endl << std::endl;

    // Main loop
    cv::Mat im;
    for (int ni = 0; ni < nImages; ni++)
    {
        if (pSLAM->isShutDown())
            break;
        // Read image and depthmap from file
        im = cv::imread(std::string(argv[4]) + "/" + vstrImageFilenamesRGB[ni], cv::IMREAD_UNCHANGED); //,cv::IMREAD_UNCHANGED);
        cv::cvtColor(im, im, CV_BGR2RGB);
        double tframe = vTimestamps[ni];

        if (im.empty())
        {
            std::cerr << std::endl << "Failed to load image at: "
                      << std::string(argv[4]) << "/" << vstrImageFilenamesRGB[ni] << std::endl;
            return 1;
        }

        if (imageScale != 1.f)
        {
            int width = im.cols * imageScale;
            int height = im.rows * imageScale;
            cv::resize(im, im, cv::Size(width, height));
        }

        std::chrono::steady_clock::time_point t1 = std::chrono::steady_clock::now();

        // Pass the image to the SLAM system
        pSLAM->TrackMonocular(im, tframe, std::vector<ORB_SLAM3::IMU::Point>(), vstrImageFilenamesRGB[ni]);

        std::chrono::steady_clock::time_point t2 = std::chrono::steady_clock::now();

        double ttrack = std::chrono::duration_cast<std::chrono::duration<double>>(t2 - t1).count();
        vTimesTrack[ni] = ttrack;

        // Wait to load the next frame
        double T = 0;
        if (ni < nImages - 1)
            T = vTimestamps[ni + 1] - tframe;
        else if (ni > 0)
            T = tframe - vTimestamps[ni - 1];

        if (ttrack < T)
            usleep((T - ttrack) * 1e6);
    }
'''
new2 = '''    // Vector for tracking time statistics
    std::vector<float> vTimesTrack;

    std::cout << std::endl << "-------" << std::endl;
    std::cout << "Start processing sequence ..." << std::endl;
    std::cout << "Incremental mode: polling " << strFile << " for new frames." << std::endl;
    std::cout << "(create a 'DONE' file in the sequence dir to signal end-of-stream)" << std::endl << std::endl;

    // Main loop (incremental)
    cv::Mat im;
    int ni = 0;
    while (true)
    {
        if (pSLAM->isShutDown())
            break;

        // Re-read rgb.txt to pick up new frames
        vstrImageFilenamesRGB.clear();
        vTimestamps.clear();
        LoadImages(strFile, vstrImageFilenamesRGB, vTimestamps);

        if ((int)vstrImageFilenamesRGB.size() <= ni)
        {
            // No new frames: check for end-of-stream sentinel
            if (std::filesystem::exists(std::filesystem::path(argv[4]) / "DONE"))
            {
                std::cout << "[M4.1] DONE sentinel found, stopping input." << std::endl;
                break;
            }
            usleep(200000);  // 200ms
            continue;
        }

        for (; ni < (int)vstrImageFilenamesRGB.size(); ni++)
        {
            // Read image from file
            im = cv::imread(std::string(argv[4]) + "/" + vstrImageFilenamesRGB[ni], cv::IMREAD_UNCHANGED);
            if (im.empty())
            {
                std::cerr << "Failed to load " << vstrImageFilenamesRGB[ni] << ", retrying next poll." << std::endl;
                usleep(100000);  // 100ms, frame may be half-written
                break;  // retry this frame on next poll (ni not incremented)
            }
            cv::cvtColor(im, im, CV_BGR2RGB);
            double tframe = vTimestamps[ni];

            if (imageScale != 1.f)
            {
                int width = im.cols * imageScale;
                int height = im.rows * imageScale;
                cv::resize(im, im, cv::Size(width, height));
            }

            std::chrono::steady_clock::time_point t1 = std::chrono::steady_clock::now();

            // Pass the image to the SLAM system
            pSLAM->TrackMonocular(im, tframe, std::vector<ORB_SLAM3::IMU::Point>(), vstrImageFilenamesRGB[ni]);

            std::chrono::steady_clock::time_point t2 = std::chrono::steady_clock::now();

            double ttrack = std::chrono::duration_cast<std::chrono::duration<double>>(t2 - t1).count();
            vTimesTrack.push_back(ttrack);
        }
    }
'''

assert s.count(old1) == 1, "old1 count = %d" % s.count(old1)
assert s.count(old2) == 1, "old2 count = %d" % s.count(old2)
s = s.replace(old1, new1).replace(old2, new2)
open(p, "w").write(s)
print("patched tum_mono.cpp OK")
