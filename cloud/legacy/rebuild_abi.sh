#!/usr/bin/env bash
set -e
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
ABI="-D_GLIBCXX_USE_CXX11_ABI=0"

echo "=== 1/3 重编 OpenCV（旧 ABI）==="
cd ~/opencv
rm -rf build && mkdir -p build && cd build
cmake -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr/local \
  -DCMAKE_CXX_FLAGS="$ABI" \
  -DCMAKE_CUDA_FLAGS="-Xcompiler $ABI" \
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
make -j8
make install
ldconfig

echo "=== 2/3 重编 jsoncpp（旧 ABI）==="
cd ~
if [ ! -d jsoncpp ]; then
  git clone --depth 1 --branch 1.9.5 https://github.com/open-source-parsers/jsoncpp.git jsoncpp 2>&1 | tail -2
fi
cd jsoncpp && rm -rf build && mkdir -p build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$ABI" \
  -DJSONCPP_WITH_TESTS=OFF -DBUILD_SHARED_LIBS=ON -DBUILD_STATIC_LIBS=OFF \
  -DCMAKE_INSTALL_PREFIX=/usr/local ..
make -j8
make install
ldconfig
echo "=== 3/3 验证 ==="
ls -la /usr/local/lib/libopencv_imgcodecs.so* /usr/local/lib/libjsoncpp.so* 2>/dev/null | head
echo "ABI_REBUILD_DONE"
