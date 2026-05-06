import torch

def check_torch_cuda_info():
    """检查 PyTorch 和 CUDA/GPU 相关信息"""
    # 1. 打印 PyTorch 基础版本
    print(f"=== PyTorch 基础信息 ===")
    print(f"PyTorch 版本：{torch.__version__}")
    print(f"TorchVision 版本：{torchvision.__version__ if 'torchvision' in globals() else '未安装'}")
    
    # 2. 检查 CUDA 可用性
    print(f"\n=== CUDA 相关信息 ===")
    print(f"CUDA 是否可用：{torch.cuda.is_available()}")
    if torch.cuda.is_available():
        # 获取 GPU 数量
        gpu_count = torch.cuda.device_count()
        print(f"可用 GPU 数量：{gpu_count}")
        
        # 遍历所有 GPU 打印详细信息
        for i in range(gpu_count):
            print(f"\n--- GPU {i} 详细信息 ---")
            print(f"GPU 名称：{torch.cuda.get_device_name(i)}")
            print(f"CUDA 设备索引：{torch.cuda.device(i)}")
            print(f"CUDA 版本：{torch.version.cuda}")
            
            # 显存信息（总显存、已用显存、可用显存）
            total_mem = torch.cuda.get_device_properties(i).total_memory / 1024**3
            used_mem = torch.cuda.memory_allocated(i) / 1024**3
            free_mem = total_mem - used_mem
            print(f"总显存：{total_mem:.2f} GB")
            print(f"已用显存：{used_mem:.2f} GB")
            print(f"可用显存：{free_mem:.2f} GB")
            
            # GPU 计算能力
            print(f"GPU 计算能力：{torch.cuda.get_device_capability(i)}")
    else:
        print("⚠️  未检测到可用的 CUDA 设备，可能原因：")
        print("   1. 未安装 CUDA 版本的 PyTorch")
        print("   2. 显卡不支持 CUDA（如集成显卡/AMD 显卡）")
        print("   3. CUDA 驱动版本与 PyTorch 不兼容")

# 先安装 torchvision（如果未安装）
try:
    import torchvision
except ImportError:
    print("正在安装 torchvision...")
    import subprocess
    subprocess.check_call(["pip", "install", "torchvision", "-q"])
    import torchvision

# 执行检查
if __name__ == "__main__":
    check_torch_cuda_info()