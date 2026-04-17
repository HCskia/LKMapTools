# 🗺️ LKMapTools (OpenCV-ORB-MapTracker)

参考项目：[761696148/Game-Map-Tracker](https://github.com/761696148/Game-Map-Tracker)

一个基于 **OpenCV ORB 特征匹配**的高性能游戏实时定位与地图追踪系统。

本项目专为高频截屏并在大尺寸逻辑地图（Logic Map）上进行实时定位的需求而设计。通过**生产者-消费者线程模型**、**特征缓存**以及**视口裁剪渲染算法**，解决了 Python 在处理高分辨率图像匹配时的性能痛点。

---

## ✨ 核心特性

- **⚡ 极致匹配性能**：
    - 采用 `ORB` 特征提取算法，配合 `BFMatcher` (Hamming Distance) 充分利用 CPU 硬件加速。
    - 引入 **局部搜索 (Local Search)** 逻辑：根据上一帧坐标动态切片，减少 90% 以上的无效搜索空间。
- **🧵 读写分离异步架构**：
    - **Capture Thread (生产者)**：独立负责高频截屏 (mss) 与图像增强，稳定控制 FPS，避免 UI 线程阻塞。
    - **Match Thread (消费者)**：专用计算线程，采用无锁队列保持最新帧，实现毫秒级坐标推算。
- **💾 特征点持久化 (Caching)**：
    - 支持将数万个大地图特征点及描述符序列化为 `.npz` 压缩包，实现程序“秒级”启动。
- **🖼️ 丝滑地图交互 (GUI)**：
    - **静态烘焙 (Baking)**：将上千个标记点预先合成至地图底层，消除 Tkinter 渲染数千个 Canvas 对象的性能瓶颈。
    - **视口裁剪 (Viewport Cropping)**：采用“先裁剪后缩放”算法，即使在 8K 或更大地图上也能维持满帧拖拽和缩放。
- **🛠️ 鲁棒性优化**：
    - 采用 `estimateAffinePartial2D` (4 自由度) 代替传统的单应性矩阵 (8 自由度)，有效过滤非线性噪声，防止定位点异常跳变。

---

## 🚀 快速开始

### 1. 环境依赖
```bash
pip install opencv-python numpy mss pillow
```

### 2.项目结构

- `main_orb.py`: 系统主入口，包含双线程管理及 GUI 实现。

- `config.py`: 核心超参数配置（定位精度、截图区域、匹配阈值等）。

- `assets/`: 放置游戏地图原图和标点图标。

- `.npz缓存文件`: (自动生成) 缓存的地图特征点数据。

### 3.运行
``` bash
python main_orb.py
```
或者直接从release下载exe文件


### 开源协议
MIT License