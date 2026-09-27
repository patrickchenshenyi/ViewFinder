set -e
# DBoW2
cd ./ORB-SLAM3/Thirdparty/DBoW2
mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4

cd ../../g2o

# g2o
mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4

cd ../../Sophus

# Sophus
mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4

# ORB_SLAM3
cd ../../../Vocabulary
echo "Uncompress vocabulary ..."
tar -xf ORBvoc.txt.tar.gz
cd ..

mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4

# Photo-SLAM
echo "Building Photo-SLAM ..."
cd ../..
mkdir -p build
cd build
cmake .. -DTorch_DIR=/root/libtorch/share/cmake/Torch -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4
