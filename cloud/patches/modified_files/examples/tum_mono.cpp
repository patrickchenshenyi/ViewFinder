/**
* This file is part of Photo-SLAM
*
* Copyright (C) 2023-2024 Longwei Li and Hui Cheng, Sun Yat-sen University.
* Copyright (C) 2023-2024 Huajian Huang and Sai-Kit Yeung, Hong Kong University of Science and Technology.
*
* Photo-SLAM is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
* License as published by the Free Software Foundation, either version 3 of the License, or
* (at your option) any later version.
*
* Photo-SLAM is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even
* the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
* GNU General Public License for more details.
*
* You should have received a copy of the GNU General Public License along with Photo-SLAM.
* If not, see <http://www.gnu.org/licenses/>.
*/

#include <torch/torch.h>

#include <iostream>
#include <algorithm>
#include <fstream>
#include <chrono>
#include <ctime>
#include <sstream>
#include <thread>
#include <filesystem>
#include <memory>

#include <opencv2/core/core.hpp>

#include "ORB-SLAM3/include/System.h"
#include "include/gaussian_mapper.h"
#include "viewer/imgui_viewer.h"

void LoadImages(const std::string &strFile, std::vector<std::string> &vstrImageFilenames,
                std::vector<double> &vTimestamps);
void saveTrackingTime(std::vector<float> &vTimesTrack, const std::string &strSavePath);
void saveGpuPeakMemoryUsage(std::filesystem::path pathSave);

int main(int argc, char **argv)
{
    if (argc != 6 && argc != 7)
    {
        std::cerr << std::endl
                  << "Usage: " << argv[0]
                  << " path_to_vocabulary"                   /*1*/
                  << " path_to_ORB_SLAM3_settings"           /*2*/
                  << " path_to_gaussian_mapping_settings"    /*3*/
                  << " path_to_sequence"                     /*4*/
                  << " path_to_trajectory_output_directory/" /*5*/
                  << " (optional)no_viewer"                  /*6*/
                  << std::endl;
        return 1;
    }
    bool use_viewer = true;
    if (argc == 7)
        use_viewer = (std::string(argv[6]) == "no_viewer" ? false : true);

    std::string output_directory = std::string(argv[5]);
    if (output_directory.back() != '/')
        output_directory += "/";
    std::filesystem::path output_dir(output_directory);

    // Retrieve paths to images (incremental mode: poll rgb.txt continuously)
    std::vector<std::string> vstrImageFilenamesRGB;
    std::vector<double> vTimestamps;
    std::string strFile = std::string(argv[4]) + "/rgb.txt";

    // Device
    torch::DeviceType device_type;
    if (torch::cuda::is_available())
    {
        std::cout << "CUDA available! Training on GPU." << std::endl;
        device_type = torch::kCUDA;
    }
    else
    {
        std::cout << "Training on CPU." << std::endl;
        device_type = torch::kCPU;
    }

    // Create SLAM system. It initializes all system threads and gets ready to process frames.
    std::shared_ptr<ORB_SLAM3::System> pSLAM =
        std::make_shared<ORB_SLAM3::System>(
            argv[1], argv[2], ORB_SLAM3::System::MONOCULAR);
    float imageScale = pSLAM->GetImageScale();

    // Create GaussianMapper
    std::filesystem::path gaussian_cfg_path(argv[3]);
    std::shared_ptr<GaussianMapper> pGausMapper =
        std::make_shared<GaussianMapper>(
            pSLAM, gaussian_cfg_path, output_dir, 0, device_type);
    std::thread training_thd(&GaussianMapper::run, pGausMapper.get());

    // Create Gaussian Viewer
    std::thread viewer_thd;
    std::shared_ptr<ImGuiViewer> pViewer;
    if (use_viewer)
    {
        pViewer = std::make_shared<ImGuiViewer>(pSLAM, pGausMapper);
        viewer_thd = std::thread(&ImGuiViewer::run, pViewer.get());
    }

    // Vector for tracking time statistics
    std::vector<float> vTimesTrack;

    std::cout << std::endl << "-------" << std::endl;
    std::cout << "Start processing sequence ..." << std::endl;
    std::cout << "Incremental mode: polling " << strFile << " for new frames." << std::endl;
    std::cout << "(create a 'DONE' file in the sequence dir to signal end-of-stream)" << std::endl << std::endl;

    // Main loop (incremental)
    cv::Mat im;
    int ni = 0;
    double lostSinceT = -1.0;             // 连续丢跟踪的起始时间戳（<0 = 未丢）
    const double kLostResetAfterS = 15.0; // 连续丢跟踪超过 15s → 重置活动地图
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

            // 测试钩子：TUM_PACE_MS>0 时每帧 usleep，让输入与实时跟踪线程同步
            // （本地回放/合成序列验证丢跟踪重置用；实时推流下默认关闭，自然按网络节奏）
            {
                static int paceMs = -1;
                if (paceMs < 0) {
                    const char* env = std::getenv("TUM_PACE_MS");
                    paceMs = (env && atoi(env) > 0) ? atoi(env) : 0;
                }
                if (paceMs > 0) usleep(paceMs * 1000);
            }

            std::chrono::steady_clock::time_point t1 = std::chrono::steady_clock::now();

            // Pass the image to the SLAM system
            pSLAM->TrackMonocular(im, tframe, std::vector<ORB_SLAM3::IMU::Point>(), vstrImageFilenamesRGB[ni]);

            // M7.5: 跟踪状态日志 + 重初始化卡住兜底。
            // 注：ORB-SLAM3 本身在「丢跟踪 3s 重定位失败」后会自动建新地图重新初始化
            //（RECENTLY_LOST→LOST→新地图），因此无需在这里再做过早重置；
            // 这里兜底「重初始化长时间卡住」（NOT_INITIALIZED>30s 且曾 OK 过）的情况。
            {
                static int lastLoggedState = -999;
                static bool hasBeenOk = false;
                static double notInitSince = -1.0;
                int trackState = pSLAM->GetTrackingState();
                if (trackState != lastLoggedState)
                {
                    std::cout << "[M7.5] 状态变化 " << lastLoggedState << " -> " << trackState
                              << " (t=" << tframe << "s, frame " << ni + 1 << ")"
                              << " [-1=未就绪 0=无图像 1=未初始化 2=OK 3=刚丢 4=丢失]" << std::endl;
                    lastLoggedState = trackState;
                }
                if (trackState == (int)ORB_SLAM3::Tracking::OK)
                    hasBeenOk = true;
                if (trackState == (int)ORB_SLAM3::Tracking::NOT_INITIALIZED && hasBeenOk)
                {
                    if (notInitSince < 0.0)
                        notInitSince = tframe;
                    else if (tframe - notInitSince > 30.0)
                    {
                        std::cout << "[M7.5] 重初始化卡住 " << (tframe - notInitSince)
                                  << "s，重置活动地图重试…" << std::endl;
                        pSLAM->ResetActiveMap();
                        notInitSince = -1.0;
                    }
                }
                else
                {
                    notInitSince = -1.0;
                }
            }


            std::chrono::steady_clock::time_point t2 = std::chrono::steady_clock::now();

            double ttrack = std::chrono::duration_cast<std::chrono::duration<double>>(t2 - t1).count();
            vTimesTrack.push_back(ttrack);
        }
    }

    // Stop all threads
    pSLAM->Shutdown();
    training_thd.join();
    if (use_viewer)
        viewer_thd.join();

    // GPU peak usage
    saveGpuPeakMemoryUsage(output_dir / "GpuPeakUsageMB.txt");

    // Tracking time statistics
    saveTrackingTime(vTimesTrack, (output_dir / "TrackingTime.txt").string());

    // Save camera trajectory
    pSLAM->SaveTrajectoryTUM((output_dir / "CameraTrajectory_TUM.txt").string());
    pSLAM->SaveKeyFrameTrajectoryTUM((output_dir / "KeyFrameTrajectory_TUM.txt").string());
    pSLAM->SaveTrajectoryEuRoC((output_dir / "CameraTrajectory_EuRoC.txt").string());
    pSLAM->SaveKeyFrameTrajectoryEuRoC((output_dir / "KeyFrameTrajectory_EuRoC.txt").string());
    // pSLAM->SaveTrajectoryKITTI((output_dir / "CameraTrajectory_KITTI.txt").string());

    return 0;
}

void LoadImages(const std::string &strFile, std::vector<std::string> &vstrImageFilenames,
                std::vector<double> &vTimestamps)
{
    ifstream f;
    f.open(strFile.c_str());

    // skip first three lines
    string s0;
    std::getline(f,s0);
    std::getline(f,s0);
    std::getline(f,s0);

    while(!f.eof())
    {
        std::string s;
        std::getline(f,s);
        if(!s.empty())
        {
            std::stringstream ss;
            ss << s;
            double t;
            std::string sRGB;
            ss >> t;
            vTimestamps.push_back(t);
            ss >> sRGB;
            vstrImageFilenames.push_back(sRGB);
        }
    }
}

void saveTrackingTime(std::vector<float> &vTimesTrack, const std::string &strSavePath)
{
    std::ofstream out;
    out.open(strSavePath.c_str());
    std::size_t nImages = vTimesTrack.size();
    float totaltime = 0;
    for (int ni = 0; ni < nImages; ni++)
    {
        out << std::fixed << std::setprecision(4)
            << vTimesTrack[ni] << std::endl;
        totaltime += vTimesTrack[ni];
    }

    // std::sort(vTimesTrack.begin(), vTimesTrack.end());
    // out << "-------" << std::endl;
    // out << std::fixed << std::setprecision(4)
    //     << "median tracking time: " << vTimesTrack[nImages / 2] << std::endl;
    // out << std::fixed << std::setprecision(4)
    //     << "mean tracking time: " << totaltime / nImages << std::endl;

    out.close();
}

void saveGpuPeakMemoryUsage(std::filesystem::path pathSave)
{
    namespace c10Alloc = c10::cuda::CUDACachingAllocator;
    c10Alloc::DeviceStats mem_stats = c10Alloc::getDeviceStats(0);

    c10Alloc::Stat reserved_bytes = mem_stats.reserved_bytes[static_cast<int>(c10Alloc::StatType::AGGREGATE)];
    float max_reserved_MB = reserved_bytes.peak / (1024.0 * 1024.0);

    c10Alloc::Stat alloc_bytes = mem_stats.allocated_bytes[static_cast<int>(c10Alloc::StatType::AGGREGATE)];
    float max_alloc_MB = alloc_bytes.peak / (1024.0 * 1024.0);

    std::ofstream out(pathSave);
    out << "Peak reserved (MB): " << max_reserved_MB << std::endl;
    out << "Peak allocated (MB): " << max_alloc_MB << std::endl;
    out.close();
}