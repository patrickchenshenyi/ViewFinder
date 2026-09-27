#!/usr/bin/env python3
# M4.2+M4.3: 给高斯加稳定 ID + delta 导出（added/removed），并在 mapper 在线更新后触发导出
import sys

def patch(path, pairs):
    s = open(path).read()
    for old, new in pairs:
        c = s.count(old)
        assert c == 1, "%s: pattern count=%d for:\n%s" % (path, c, old[:100])
        s = s.replace(old, new)
    open(path, "w").write(s)
    print("patched %s OK" % path)

# ---------- 1) gaussian_model.h ----------
patch("/root/Photo-SLAM/include/gaussian_model.h", [
    (
        "    torch::Tensor exist_since_iter_;\n",
        "    torch::Tensor exist_since_iter_;\n"
        "    torch::Tensor gaussian_id_;                     // stable per-gaussian id (int64)\n"
        "    long long next_gaussian_id_ = 0;                 // next id to assign (monotonic)\n"
        "    long long last_exported_max_id_ = -1;            // max id already exported\n"
        "    std::vector<long long> removed_gaussian_ids_;    // ids pruned since last delta export\n",
    ),
    (
        "    void saveSparsePointsPly(std::filesystem::path result_path);\n",
        "    void saveSparsePointsPly(std::filesystem::path result_path);\n"
        "    void saveSplatDelta(std::filesystem::path result_dir, const int iteration);\n",
    ),
])

# ---------- 2) gaussian_model.cpp ----------
patch("/root/Photo-SLAM/src/gaussian_model.cpp", [
    # createFromPcd: init id tensor + counters
    (
        "    this->exist_since_iter_ = torch::zeros(\n"
        "        {fused_point_cloud.size(0)},\n"
        "        torch::TensorOptions().dtype(torch::kInt32).device(device_type_));\n",
        "    this->exist_since_iter_ = torch::zeros(\n"
        "        {fused_point_cloud.size(0)},\n"
        "        torch::TensorOptions().dtype(torch::kInt32).device(device_type_));\n"
        "\n"
        "    this->gaussian_id_ = torch::arange(\n"
        "        this->next_gaussian_id_, this->next_gaussian_id_ + num_points,\n"
        "        torch::TensorOptions().dtype(torch::kLong).device(device_type_));\n"
        "    this->next_gaussian_id_ += num_points;\n"
        "    this->last_exported_max_id_ = -1;  // next delta export marks all current as added\n",
    ),
    # prunePoints: record removed ids
    (
        "    auto valid_points_mask = ~mask;\n",
        "    auto valid_points_mask = ~mask;\n"
        "\n"
        "    // Record pruned gaussian ids (mask=true => removed)\n"
        "    if (this->gaussian_id_.numel() > 0) {\n"
        "        torch::Tensor removed_ids = this->gaussian_id_.masked_select(mask).to(torch::kCPU);\n"
        "        auto acc = removed_ids.accessor<long long, 1>();\n"
        "        for (long long i = 0; i < acc.size(0); ++i)\n"
        "            this->removed_gaussian_ids_.push_back(acc[i]);\n"
        "    }\n",
    ),
    # prunePoints: prune id tensor
    (
        "    this->exist_since_iter_ = this->exist_since_iter_.index({valid_points_mask});\n",
        "    this->exist_since_iter_ = this->exist_since_iter_.index({valid_points_mask});\n"
        "    this->gaussian_id_ = this->gaussian_id_.index({valid_points_mask});\n",
    ),
    # densificationPostfix: append new ids
    (
        "    this->exist_since_iter_ = torch::cat({this->exist_since_iter_, new_exist_since_iter}, /*dim=*/0);\n",
        "    this->exist_since_iter_ = torch::cat({this->exist_since_iter_, new_exist_since_iter}, /*dim=*/0);\n"
        "\n"
        "    {\n"
        "        long long n_new = new_exist_since_iter.size(0);\n"
        "        torch::Tensor new_ids = torch::arange(\n"
        "            this->next_gaussian_id_, this->next_gaussian_id_ + n_new,\n"
        "            torch::TensorOptions().dtype(torch::kLong).device(device_type_));\n"
        "        this->gaussian_id_ = torch::cat({this->gaussian_id_, new_ids}, /*dim=*/0);\n"
        "        this->next_gaussian_id_ += n_new;\n"
        "    }\n",
    ),
])

# ---------- 3) append saveSplatDelta impl to gaussian_model.cpp ----------
s = open("/root/Photo-SLAM/src/gaussian_model.cpp").read()
if "void GaussianModel::saveSplatDelta" not in s:
    s += r'''

void GaussianModel::saveSplatDelta(std::filesystem::path result_dir, const int iteration)
{
    std::filesystem::create_directories(result_dir / "deltas");

    torch::Tensor ids_cpu = this->gaussian_id_.detach().to(torch::kCPU);
    auto ids_acc = ids_cpu.accessor<long long, 1>();
    long long n_total = ids_cpu.size(0);

    std::vector<long long> added_ids;
    for (long long i = 0; i < n_total; ++i)
        if (ids_acc[i] > this->last_exported_max_id_)
            added_ids.push_back(ids_acc[i]);

    std::vector<long long> removed_ids = this->removed_gaussian_ids_;
    this->removed_gaussian_ids_.clear();

    for (auto id : added_ids)
        if (id > this->last_exported_max_id_)
            this->last_exported_max_id_ = id;

    if (added_ids.empty() && removed_ids.empty())
        return;

    std::ofstream out(result_dir / "deltas" / ("delta_" + std::to_string(iteration) + ".txt"));
    out << "iteration " << iteration << "\n";
    out << "total " << n_total << "\n";
    out << "added " << added_ids.size() << "\n";
    for (auto id : added_ids) out << id << " ";
    out << "\n";
    out << "removed " << removed_ids.size() << "\n";
    for (auto id : removed_ids) out << id << " ";
    out << "\n";
    out.close();
}
'''
    open("/root/Photo-SLAM/src/gaussian_model.cpp", "w").write(s)
    print("appended saveSplatDelta OK")
else:
    print("saveSplatDelta already present")

# ---------- 4) gaussian_mapper.cpp: trigger export ----------
patch("/root/Photo-SLAM/src/gaussian_mapper.cpp", [
    (
        "        if (hasMetIncrementalMappingConditions()) {\n"
        "            combineMappingOperations();\n"
        "            if (cull_keyframes_)\n"
        "                cullKeyframes();\n"
        "        }\n",
        "        if (hasMetIncrementalMappingConditions()) {\n"
        "            combineMappingOperations();\n"
        "            if (cull_keyframes_)\n"
        "                cullKeyframes();\n"
        "            // M4.3: export splat delta (online) after integrating new mapping operations\n"
        "            gaussians_->saveSplatDelta(result_dir_, getIteration());\n"
        "        }\n",
    ),
])

print("ALL PATCHES DONE")
