import json
import os
import sys

# ==========================================
# 核心黑科技：兼容 PyInstaller 打包后的路径寻找
# ==========================================
if getattr(sys, 'frozen', False):
    # 如果是打包后的 .exe 运行，去 exe 所在的同级目录找配置文件
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # 如果是在代码编辑器里直接运行 main.py，去当前代码所在的目录找
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# ==========================================
# 默认配置字典 (如果 JSON 文件丢失，用来兜底并重新生成)
# ==========================================
# DEFAULT_CONFIG = {
#     "MINIMAP": {"top": 292, "left": 1853, "width": 150, "height": 150},
#     "WINDOW_GEOMETRY": "400x400+1500+100",
#     "VIEW_SIZE": 400,
#     "LOGIC_MAP_PATH": "big_map.png",
#     "DISPLAY_MAP_PATH": "big_map-1.png",
#     "MAX_LOST_FRAMES": 50,
#
#     "SIFT_REFRESH_RATE": 50,
#     "SIFT_CLAHE_LIMIT": 3.0,
#     "SIFT_MATCH_RATIO": 0.9,
#     "SIFT_MIN_MATCH_COUNT": 5,
#     "SIFT_RANSAC_THRESHOLD": 8.0,
#
#     "AI_REFRESH_RATE": 200,
#     "AI_CONFIDENCE_THRESHOLD": 0.6,
#     "AI_MIN_MATCH_COUNT": 6,
#     "AI_RANSAC_THRESHOLD": 8.0,
#     "AI_SCAN_SIZE": 1600,
#     "AI_SCAN_STEP": 1400,
#     "AI_TRACK_RADIUS": 500
# }

DEFAULT_CONFIG = {
    "DEBUGMODE": False, # 调试模式
    "MINIMAP": {},
    "MATCHTYPE":"BF",
    "WINDOW_GEOMETRY": "400x550+1500+100",
    "VIEW_SIZE": 400,
    "LOGIC_MAP_PATH": r"assest/raw.png",
    "DISPLAY_MAP_PATH": r"assest/raw.png",
    "MAX_LOST_FRAMES": 50, # 定位最大丢失帧数，不建议修改
    "POINTS_PATH": r"assest/points.json",
    "PICKINGDATA_PATH": r"assest/picking_data.json",
    "PICKING_RADIUS": 25, # 采集图标变换范围，也就是黄色虚线圈范围
    "ICON_PATH": r"assest/icons",
    "FEATURES_PATH": r"assest/ORB_features.npz",
    "MAX_KP_PER_LAYER": 100000, # 强制每层只保留前 N 个最强点

    "ORB_NFEATURES": 300000, #锚点总数，因为会切分区块所以此选项只能当比例而不是具体数值
    "ORB_MINI_NFEATURES": 2500, # 小地图的锚点匹配数量，越多定位越快但性能消耗越大
    "ORB_GRID":(120,120), # 地图切分区块大小，两个值尽量一致，值越大越容易匹配到锚点
    "ORB_MIN_MATCH_COUNT": 5, # 锚点匹配个数，越多定位越准越慢
    "ORB_SCALEFACTOR": 1.2, #缩放比例
    "ORB_NLEVELS": 8, #搜索层次，
    "ORB_FASTTHRESHOLD": 5, #阈值差异
    "ORB_EDGETHRESHOLD": 25, #边缘阈值，指靠近图像边界多少像素内不提取特征
    "ORB_MAP_PATH": r"assest/raw.png",
    "ORB_MAP_NOEDGE_PATH": r"assest/raw_noedge.png",
    "ORB_CLIPLIMIT": 2.0, #数值越小，对误匹配的容忍度越低，定位越稳
    "ORB_RATIO":0.75, # 阈值通常需要调高一点，比如 0.75 到 0.8，值越小定位越准越慢
    "ORB_RANSAC_THRESHOLD":5.0,
    "ORB_REFRESH_RATE": 20,
    "ORB_CLAHE_LIMIT": 3.0
}


def load_config():
    """读取 JSON 配置文件，如果没有则自动生成"""
    if not os.path.exists(CONFIG_FILE):
        print("未找到 config.json，正在自动生成默认配置文件...")
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"生成配置文件失败: {e}")
        return DEFAULT_CONFIG

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            user_config = json.load(f)

            # 巧妙的合并逻辑：防止用户在 JSON 里少填了某个字段导致程序崩溃
            merged_config = DEFAULT_CONFIG.copy()
            merged_config.update(user_config)
            return merged_config
    except Exception as e:
        print(f"⚠️ 读取 config.json 失败 (格式错误?)，将临时使用默认配置！错误: {e}")
        return DEFAULT_CONFIG


# ==========================================
# 加载配置并导出变量 (让 main.py 可以直接 import 这些变量)
# ==========================================
settings = load_config()

# 通用设置
DEBUGMODE = settings["DEBUGMODE"]
MINIMAP = settings.get("MINIMAP")
MATCHTYPE = settings.get("MATCHTYPE")
WINDOW_GEOMETRY = settings.get("WINDOW_GEOMETRY")
VIEW_SIZE = settings.get("VIEW_SIZE")
LOGIC_MAP_PATH = settings.get("LOGIC_MAP_PATH")
DISPLAY_MAP_PATH = settings.get("DISPLAY_MAP_PATH")
MAX_LOST_FRAMES = settings.get("MAX_LOST_FRAMES")
MAX_KP_PER_LAYER = settings.get("MAX_KP_PER_LAYER")
POINTS_PATH = settings.get("POINTS_PATH")
ICON_PATH = settings.get("ICON_PATH")
PICKINGDATA_PATH = settings.get("PICKINGDATA_PATH")
PICKING_RADIUS = settings.get("PICKING_RADIUS")
FEATURES_PATH = settings.get("FEATURES_PATH")

# ORB专属
ORB_MINI_NFEATURES = settings.get("ORB_MINI_NFEATURES")
ORB_MAP_PATH = settings.get("ORB_MAP_PATH")
ORB_MAP_NOEDGE_PATH = settings.get("ORB_MAP_NOEDGE_PATH")
ORB_MIN_MATCH_COUNT = settings.get("ORB_MIN_MATCH_COUNT")
ORB_NFEATURES = settings.get("ORB_NFEATURES")
ORB_SCALEFACTOR = settings.get("ORB_SCALEFACTOR")
ORB_EDGETHRESHOLD = settings.get("ORB_EDGETHRESHOLD")
ORB_NLEVELS = settings.get("ORB_NLEVELS")
ORB_RATIO = settings.get("ORB_RATIO")
ORB_CLIPLIMIT = settings.get("ORB_CLIPLIMIT")
ORB_FASTTHRESHOLD = settings.get("ORB_FASTTHRESHOLD")
ORB_RANSAC_THRESHOLD = settings.get("ORB_RANSAC_THRESHOLD")
ORB_REFRESH_RATE = settings.get("ORB_REFRESH_RATE")
ORB_CLAHE_LIMIT = settings.get("ORB_CLAHE_LIMIT")
ORB_GRID = settings.get("ORB_GRID")