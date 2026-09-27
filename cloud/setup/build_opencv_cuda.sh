#!/usr/bin/env bash
set -e
cd ~
echo "=== 1/5 下载 opencv_contrib 4.10.0 ==="
if [ ! -d opencv_contrib ]; then
  git clone --depth 1 --branch 4.10.0 --progress https://github.com/opencv/opencv_contrib.git opencv_contrib 2>&1 | tail -3
fi
cd opencv
rm -rf build && mkdir -p build && cd build
echo "=== 2/5 cmake 配置（含 cudev）==="
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
cmake -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr/local \
  -DWITH_CUDA=ON \
  -DCUDA_TOOLKIT_ROOT_DIR=/usr/local/cuda-12.1 \
  -DCUDA_ARCH_BIN=8.9 \
  -DWITH_CUDNN=OFF -DOPENCV_DNN_CUDA=OFF -DWITH_IPP=OFF \
  -DOPENCV_EXTRA_MODULES_PATH=/root/opencv_contrib/modules \
  -DBUILD_LIST=core,imgproc,imgcodecs,calib3d,features2d,flann,video,videoio,highgui,cudev,cudawarping,cudaimgproc,cudastereo,cudaarithm,cudafilters \
  -DBUILD_opencv_python3=OFF -DBUILD_opencv_python_bindings_generator=OFF \
  -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_opencv_apps=OFF \
  -DBUILD_JAVA=OFF -DBUILD_opencv_js=OFF \
  ..
echo "=== 3/5 make -j8 ==="
make -j8
echo "=== 4/5 make install ==="
make install
ldconfig
echo "=== 5/5 验证 ==="
ls -la /usr/local/include/opencv4/opencv2/cudawarping.hpp /usr/local/include/opencv4/opencv2/cudaimgproc.hpp /usr/local/include/opencv4/opencv2/cudastereo.hpp
echo "OPENCV_BUILD_DONE"
