#!/usr/bin/env python3
# M7.1: 把 saveSplatDelta 从「仅 id 文本」升级为「二进制 delta（added 完整 62 float + removed id）」，
#       并新增 saveSplatFull（id-indexed 全量同步，供 WS 0x00 全量推送）。
# 62 float 布局与 savePly 完全一致（已实测核对 snapshot.ply 头）：
#   xyz(3) + nx,ny,nz(3,恒0) + f_dc(3) + f_rest(45) + opacity(1) + scale(3) + rot_quat(4)
# 用法：python3 m7.1_patch_save_splat_delta.py [/root/Photo-SLAM]    （默认 /root/Photo-SLAM）
# 改完需重编 bin/tum_mono；部署后必须跑「重建 PLY 与 snapshot.ply 逐字节 diff」验收（见脚本尾部注释）。
import sys
import os

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/root/Photo-SLAM"
H = os.path.join(ROOT, "include", "gaussian_model.h")
CPP = os.path.join(ROOT, "src", "gaussian_model.cpp")
MAPPER = os.path.join(ROOT, "src", "gaussian_mapper.cpp")

def patch(path, pairs):
    s = open(path).read()
    for old, new in pairs:
        c = s.count(old)
        assert c == 1, "%s: pattern count=%d for:\n%s" % (path, c, old[:200])
        s = s.replace(old, new)
    open(path, "w").write(s)
    print("patched %s OK" % path)

# ---------- 1) gaussian_model.h：加 saveSplatFull 声明 ----------
patch(H, [
    (
        "    void saveSplatDelta(std::filesystem::path result_dir, const int iteration);\n",
        "    void saveSplatDelta(std::filesystem::path result_dir, const int iteration);\n"
        "    void saveSplatFull(std::filesystem::path result_dir, const int iteration);\n",
    ),
])

# ---------- 2) gaussian_model.cpp：重写 saveSplatDelta + 追加 saveSplatFull ----------
# 定位到已追加在文件末尾的 saveSplatDelta，从它开始整体替换为新实现（helper + delta + full）。
s = open(CPP).read()
anchor = "void GaussianModel::saveSplatDelta"
idx = s.find(anchor)
assert idx >= 0, "%s: saveSplatDelta not found" % CPP
s = s[:idx] + r'''// M7.1: 序列化一个高斯的 62 个 float（savePly 顺序，小端；云 x86_64 与浏览器均小端）。
// 参数指针为 CPU 连续张量的数据指针；frest 形状 (N,15,3) 连续，通道主序写出：
//   f_rest_[c*15+r] = frest[r*3+c]，等价 savePly 的 features_rest.transpose(1,2).flatten()。
static void writeGaussian62(std::ofstream& out,
                            const float* xyz, const float* fdc, const float* frest,
                            float opacity, const float* scale, const float* rot)
{
    auto wf = [&](float v) { out.write(reinterpret_cast<const char*>(&v), 4); };
    for (int k = 0; k < 3; ++k) wf(xyz[k]);            // x,y,z
    for (int k = 0; k < 3; ++k) wf(0.0f);             // nx,ny,nz（savePly 恒 0）
    for (int k = 0; k < 3; ++k) wf(fdc[k]);           // f_dc_0..2
    for (int c = 0; c < 3; ++c)                       // f_rest_0..44（通道主序 c*15+r）
        for (int r = 0; r < 15; ++r) wf(frest[r * 3 + c]);
    wf(opacity);                                      // opacity
    for (int k = 0; k < 3; ++k) wf(scale[k]);         // scale_0..2
    for (int k = 0; k < 4; ++k) wf(rot[k]);           // rot_0..3
}

void GaussianModel::saveSplatDelta(std::filesystem::path result_dir, const int iteration)
{
    std::filesystem::create_directories(result_dir / "deltas");

    torch::Tensor ids_cpu = this->gaussian_id_.detach().to(torch::kCPU);
    auto ids_acc = ids_cpu.accessor<int64_t, 1>();
    int64_t n_total = ids_cpu.size(0);

    // 收集 added 的 id 与其张量下标（稳妥起见不假设「added=后缀」，用下标映射）
    std::vector<int64_t> added_ids, added_idx;
    for (int64_t i = 0; i < n_total; ++i)
        if (ids_acc[i] > this->last_exported_max_id_) {
            added_ids.push_back(ids_acc[i]);
            added_idx.push_back(i);
        }

    std::vector<int64_t> removed_ids = this->removed_gaussian_ids_;
    this->removed_gaussian_ids_.clear();

    for (auto id : added_ids)
        if (id > this->last_exported_max_id_)
            this->last_exported_max_id_ = id;

    if (added_ids.empty() && removed_ids.empty())
        return;

    torch::Tensor xyz     = this->xyz_.detach().to(torch::kCPU).contiguous();
    torch::Tensor fdc     = this->features_dc_.detach().to(torch::kCPU).contiguous();
    torch::Tensor frest   = this->features_rest_.detach().to(torch::kCPU).contiguous();
    torch::Tensor opacity = this->opacity_.detach().to(torch::kCPU).contiguous();
    torch::Tensor scale   = this->scaling_.detach().to(torch::kCPU).contiguous();
    torch::Tensor rot     = this->rotation_.detach().to(torch::kCPU).contiguous();

    const float* xyz_p     = xyz.data_ptr<float>();
    const float* fdc_p     = fdc.data_ptr<float>();
    const float* frest_p   = frest.data_ptr<float>();
    const float* opacity_p = opacity.data_ptr<float>();
    const float* scale_p   = scale.data_ptr<float>();
    const float* rot_p     = rot.data_ptr<float>();

    std::ofstream out(result_dir / "deltas" / ("delta_" + std::to_string(iteration) + ".bin"),
                      std::ios::binary);
    uint32_t added_n   = (uint32_t)added_idx.size();
    uint32_t removed_n = (uint32_t)removed_ids.size();
    out.write(reinterpret_cast<const char*>(&added_n), 4);
    out.write(reinterpret_cast<const char*>(&removed_n), 4);
    for (size_t k = 0; k < added_idx.size(); ++k) {
        int64_t i = added_idx[k];
        int64_t id = ids_acc[i];
        out.write(reinterpret_cast<const char*>(&id), 8);
        writeGaussian62(out, xyz_p + i * 3, fdc_p + i * 3, frest_p + i * 45,
                        opacity_p[i], scale_p + i * 3, rot_p + i * 4);
    }
    for (auto id : removed_ids)
        out.write(reinterpret_cast<const char*>(&id), 8);
    out.close();
}

void GaussianModel::saveSplatFull(std::filesystem::path result_dir, const int iteration)
{
    std::filesystem::create_directories(result_dir / "deltas");

    torch::Tensor ids_cpu = this->gaussian_id_.detach().to(torch::kCPU);
    auto ids_acc = ids_cpu.accessor<int64_t, 1>();
    int64_t n_total = ids_cpu.size(0);

    torch::Tensor xyz     = this->xyz_.detach().to(torch::kCPU).contiguous();
    torch::Tensor fdc     = this->features_dc_.detach().to(torch::kCPU).contiguous();
    torch::Tensor frest   = this->features_rest_.detach().to(torch::kCPU).contiguous();
    torch::Tensor opacity = this->opacity_.detach().to(torch::kCPU).contiguous();
    torch::Tensor scale   = this->scaling_.detach().to(torch::kCPU).contiguous();
    torch::Tensor rot     = this->rotation_.detach().to(torch::kCPU).contiguous();

    const float* xyz_p     = xyz.data_ptr<float>();
    const float* fdc_p     = fdc.data_ptr<float>();
    const float* frest_p   = frest.data_ptr<float>();
    const float* opacity_p = opacity.data_ptr<float>();
    const float* scale_p   = scale.data_ptr<float>();
    const float* rot_p     = rot.data_ptr<float>();

    std::ofstream out(result_dir / "deltas" / ("full_" + std::to_string(iteration) + ".bin"),
                      std::ios::binary);
    uint32_t count = (uint32_t)n_total;
    out.write(reinterpret_cast<const char*>(&count), 4);
    int64_t max_id = -1;
    for (int64_t i = 0; i < n_total; ++i) {
        int64_t id = ids_acc[i];
        if (id > max_id) max_id = id;
        out.write(reinterpret_cast<const char*>(&id), 8);
        writeGaussian62(out, xyz_p + i * 3, fdc_p + i * 3, frest_p + i * 45,
                        opacity_p[i], scale_p + i * 3, rot_p + i * 4);
    }
    out.close();

    // 全量同步后把增量基线归零：后续 delta 只含「全量之后的」新增/删除。
    this->last_exported_max_id_ = max_id;
    this->removed_gaussian_ids_.clear();
}
'''
open(CPP, "w").write(s)
print("rewrote saveSplatDelta + appended saveSplatFull OK")

# ---------- 3) gaussian_mapper.cpp：周期快照 + 两处最终结果处都写 id-indexed 全量 ----------
# 3a) 周期快照（%300，唯一）
patch(MAPPER, [
    (
        "        // M4.4: periodic live snapshot for Web push (overwrite the same file)\n"
        "        if (getIteration() % 300 == 0) {\n"
        "            std::filesystem::create_directories(result_dir_ / \"live\");\n"
        "            gaussians_->savePly(result_dir_ / \"live\" / \"snapshot.ply\");\n"
        "        }\n",
        "        // M4.4: periodic live snapshot for Web push (overwrite the same file)\n"
        "        if (getIteration() % 300 == 0) {\n"
        "            std::filesystem::create_directories(result_dir_ / \"live\");\n"
        "            gaussians_->savePly(result_dir_ / \"live\" / \"snapshot.ply\");\n"
        "            gaussians_->saveSplatFull(result_dir_, getIteration());  // M7.1: id-indexed 全量同步\n"
        "        }\n",
    ),
])
# 3b) 最终结果（M5.2 两处 shutdown 路径，文本相同 → 各替换一次）
s = open(MAPPER).read()
final_old = (
    "    // M5.2: 最终结果也写到 live/snapshot.ply，供 WS 推浏览器显示最终版\n"
    "    std::filesystem::create_directories(result_dir_ / \"live\");\n"
    "    gaussians_->savePly(result_dir_ / \"live\" / \"snapshot.ply\");\n"
)
final_new = (
    "    // M5.2: 最终结果也写到 live/snapshot.ply，供 WS 推浏览器显示最终版\n"
    "    std::filesystem::create_directories(result_dir_ / \"live\");\n"
    "    gaussians_->savePly(result_dir_ / \"live\" / \"snapshot.ply\");\n"
    "    gaussians_->saveSplatFull(result_dir_, getIteration());  // M7.1: id-indexed 全量同步\n"
)
c = s.count(final_old)
assert c == 2, "%s: final-hook pattern count=%d (expected 2)" % (MAPPER, c)
s = s.replace(final_old, final_new)
open(MAPPER, "w").write(s)
print("patched %s OK (final hooks x2)" % MAPPER)

print("ALL M7.1 PATCHES DONE")
print("部署后验收：从 full_*.bin 重建 PLY 并与 snapshot.ply 逐字节 diff（见 scripts/m7_rebuild_check.py）。")
