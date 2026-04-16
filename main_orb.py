# --- DPI 声明 -----------------------------------------------------
import sys
if sys.platform == "win32":
    import ctypes
    try:
        # 强制接管物理像素 (Win10/Win11)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            # 退化方案 (Win8)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            # 最终兜底 (Win7)
            ctypes.windll.user32.SetProcessDPIAware()

# ------------------------------------------------------------------

import json
import threading
import queue
import traceback
import cv2
import numpy as np
import mss
import ctypes
from pynput import keyboard
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from PIL import Image, ImageTk, ImageDraw
import time
import config  # <--- 导入同目录下的配置文件
import subprocess
import os


# Windows API 常量
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

# 参数
DEBUG_MODE = config.DEBUGMODE
MIN_MATCH_COUNT = config.ORB_MIN_MATCH_COUNT  # 增加最小匹配数要求
CONFIG_FILE = "config.json"
MATCHTYPE = config.MATCHTYPE
selector_event = threading.Event()
WINDOW_GEOMETRY = "400x660+1500+100"
resource_type_dicts = {
    "矿物资源":(701,704),
    "非矿物资源": (705,737),
    "宝箱":(301,322),
    "眠枭之星":(802,803)
}

def super_enhance(image, isPlayer=False): #return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # 转为灰度
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    alpha = 1.2  # 对比度系数
    beta = -40  # 亮度偏移
    enhanced = cv2.convertScaleAbs(gray, alpha=alpha, beta=beta)
    return enhanced


class BigMapWindow(tk.Toplevel):
    def __init__(self, master, map_img, markers, icon_cache, resource_type_selected_items):
        super().__init__(master)
        self.title("全图预览 - 鼠标滚轮缩放 / 左键拖拽")
        self.geometry("1000x800")

        self.original_img = map_img.convert("RGBA")  # PIL Image 对象
        self.markers = markers
        self.icon_cache = icon_cache

        # 烘焙原尺寸大图 (包含图标)
        self.resource_type_selected_items = resource_type_selected_items
        self.baked_full_image = self.bake_static_map()
        self.orig_w, self.orig_h = self.baked_full_image.size

        # 生成缩略图缓存 (用于极度缩小的情况，提升画质和性能)
        # 设定缩略图最大边长为 2048
        thumb_ratio = min(2048 / self.orig_w, 2048 / self.orig_h)
        if thumb_ratio < 1.0:
            self.thumbnail_img = self.baked_full_image.resize(
                (int(self.orig_w * thumb_ratio), int(self.orig_h * thumb_ratio)),
                Image.Resampling.BILINEAR
            )
            self.thumb_scale_factor = thumb_ratio
        else:
            self.thumbnail_img = self.baked_full_image
            self.thumb_scale_factor = 1.0

        self.is_dragging = False  # 初始化拖拽状态
        self.scale = 0.2  # 初始缩放比例（全图通常很大，默认缩小显示）
        self.offset_x = 0
        self.offset_y = 0

        # 将视角居中
        self.offset_x = (self.winfo_width() - self.orig_w * self.scale) / 2
        self.offset_y = (self.winfo_height() - self.orig_h * self.scale) / 2

        self.canvas = tk.Canvas(self, bg='#1a1a1a', cursor="fleur")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 绑定事件
        self.canvas.bind("<MouseWheel>", self.on_zoom)
        self.canvas.bind("<ButtonPress-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_drag_release)

        # 窗口大小改变时重新渲染
        self.bind("<Configure>", lambda e: self.render())

        self.after(300, self.render) # 绘制刷新时间

    def bake_static_map(self):
        """将所有图标预先绘制到大图上，生成一个静态图层"""
        log_step("正在预渲染大地图图标...")
        # 拷贝一份原图，避免破坏原始数据
        working_img = self.original_img.copy()

        for m in self.markers:
            icon_set = self.icon_cache.get(str(m['type']))
            if not icon_set: continue

            m_type = m['type']
            try:
                m_type_int = int(m_type)
            except ValueError:
                continue  # 如果还是遇到了非数字，安全跳过该点，不要引发系统崩溃

            is_visible = False
            for category_name in self.resource_type_selected_items:
                # 获取该分类对应的 ID 范围
                range_min, range_max = resource_type_dicts.get(category_name, (0, 0))
                if range_min <= m_type_int <= range_max:
                    is_visible = True
                    break

            if not is_visible:
                continue

            # 根据状态选择图标
            icon = icon_set["pil_gray"] if m.get('is_collected') else icon_set["pil_normal"]

            # 计算粘贴位置 (图标中心点对齐地图坐标)
            ix, iy = m['pixel_x'], m['pixel_y']
            iw, ih = icon.size
            # paste 的坐标是左上角
            paste_pos = (int(ix - iw // 2), int(iy - ih // 2))

            # 粘贴图标（使用图标自身的 alpha 通道作为 mask）
            working_img.paste(icon, paste_pos, icon)

        log_step("预渲染完成。")
        return working_img

    def render(self):
        """视口裁剪算法：只计算并渲染当前屏幕可见的区域"""

        # 确保必要属性已加载
        if not hasattr(self, 'canvas') or not hasattr(self, 'is_dragging'):
            return

        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 10 or ch <= 10: return

        # 计算当前窗口在【原始大图】坐标系下的 Bounding Box
        # offset_x 是画板左上角相对于原图起点的偏移
        left = -self.offset_x / self.scale
        top = -self.offset_y / self.scale
        right = left + (cw / self.scale)
        bottom = top + (ch / self.scale)

        # 根据缩放比例决定使用原图还是缩略图来裁剪
        # 当缩放比例很小时，原图裁剪会导致锯齿，且范围过大。此时使用缩略图
        use_thumbnail = self.scale < (self.thumb_scale_factor * 1.5)

        if use_thumbnail:
            source_img = self.thumbnail_img
            # 将坐标系转换到缩略图的尺度
            left *= self.thumb_scale_factor
            top *= self.thumb_scale_factor
            right *= self.thumb_scale_factor
            bottom *= self.thumb_scale_factor
        else:
            source_img = self.baked_full_image

        # 边界限制处理
        src_w, src_h = source_img.size
        crop_left = max(0, int(left))
        crop_top = max(0, int(top))
        crop_right = min(src_w, int(right))
        crop_bottom = min(src_h, int(bottom))

        # 如果画面完全不在视野内，清空画布并跳过
        if crop_left >= src_w or crop_top >= src_h or crop_right <= 0 or crop_bottom <= 0:
            self.canvas.delete("map_img")
            return

        # 执行裁剪 (极速操作)
        cropped_img = source_img.crop((crop_left, crop_top, crop_right, crop_bottom))

        # 计算裁剪后的图像在屏幕上应该显示的尺寸和位置
        # 由于边界限制，裁剪的区域可能比窗口小，需要算出它在屏幕上的精确绘制起点
        draw_w = int((crop_right - crop_left) * (self.scale / (self.thumb_scale_factor if use_thumbnail else 1.0)))
        draw_h = int((crop_bottom - crop_top) * (self.scale / (self.thumb_scale_factor if use_thumbnail else 1.0)))

        # 计算在 Canvas 上的绘制起点
        draw_x = max(0, self.offset_x)
        draw_y = max(0, self.offset_y)

        # 最终缩放并推送到 UI
        is_dragging = getattr(self, 'is_dragging', False)
        resample_mode = Image.Resampling.NEAREST if self.is_dragging else Image.Resampling.BILINEAR
        display_img = cropped_img.resize((draw_w, draw_h), resample_mode)

        self.tk_img = ImageTk.PhotoImage(display_img)
        self.canvas.delete("map_img")
        self.canvas.create_image(draw_x, draw_y, anchor=tk.NW, image=self.tk_img, tags="map_img")

    def on_zoom(self, event):
        """以鼠标位置为中心的缩放算法"""
        old_scale = self.scale
        # 缩放因子
        factor = 1.2 if event.delta > 0 else 0.8
        self.scale *= factor

        # 限制缩放范围防止崩溃
        self.scale = max(0.02, min(self.scale, 3.0))

        # 计算偏移补偿：使得鼠标指向的那个地图点，在缩放后依然在鼠标位置
        # 核心逻辑：new_offset = mouse_pos - (mouse_pos - old_offset) * (new_scale / old_scale)
        actual_factor = self.scale / old_scale
        self.offset_x = event.x - (event.x - self.offset_x) * actual_factor
        self.offset_y = event.y - (event.y - self.offset_y) * actual_factor

        self.render()

    def on_drag_start(self, event):
        self.last_mouse_x = event.x
        self.last_mouse_y = event.y

    def on_drag_move(self, event):
        dx = event.x - self.last_mouse_x
        dy = event.y - self.last_mouse_y

        self.offset_x += dx
        self.offset_y += dy

        self.last_mouse_x = event.x
        self.last_mouse_y = event.y

        # --- 优化核心 ---
        # 不调用 self.render()，而是直接使用 canvas 硬件加速移动已有元素
        # 这样不会触发 PIL 的 resize 和 1400 个图标的重绘
        self.canvas.move("all", dx, dy)

    def on_drag_release(self, event):
        # 只有在鼠标松开时，才进行一次完整的 render 计算坐标对齐
        self.render()

class MapTrackerApp:
    def __init__(self, root):
        log_step("正在尝试建立主root")
        self.root = root
        self.root.title("ORB算法小地图定位工具")
        self.status_text_id = None

        # --- 窗口属性设置 ---
        self.root.attributes("-topmost", True)
        # --- 使用配置文件中的悬浮窗几何设置 ---
        self.root.geometry(WINDOW_GEOMETRY)
        # --- 使用配置文件中的截图区域
        self.minimap_region = config.MINIMAP
        self.last_pos = None

        # --- UI初始化 ---
        log_step("正在初始化UI")
        self.status_label = tk.Label(root, text="软件加载，请稍候...", fg="white", bg="black")
        self.status_label.pack()
        self.root.after(100, self.ui_delayed_init)

        # UI 组件
        # --- 使用配置文件中的悬浮窗视野大小 (VIEW_SIZE)
        log_step("尝试加载UI组件")
        self.canvas = tk.Canvas(root, width=config.VIEW_SIZE, height=config.VIEW_SIZE, bg='#2b2b2b')
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.image_on_canvas = None

        # UI锁定-用于全屏
        # -- 开启UI锁定
        self.ui_lock_var = tk.BooleanVar(value=False)
        self.ui_lock_cb = tk.Checkbutton(
            root,
            text="开启UI锁定【快捷键:alt+L】*需要管理员运行程序",
            variable=self.ui_lock_var,
            bg='#2b2b2b',
            fg='white',
            selectcolor='#3c3f41',
            activebackground='#2b2b2b',
            activeforeground='white',
            command = self.toggle_ui_lock_from_cb  # 绑定点击事件
        )
        self.ui_lock_cb.pack(side=tk.BOTTOM, fill=tk.X)
        self.start_hotkey_listener() #启用快捷键监听

        # 重置按钮
        log_step("正在加载按钮")
        self.reset_btn = tk.Button(
            root,
            text="手动重置定位 (全图扫描)",
            command=self.reset_location,
            bg='#3c3f41',
            fg='white',
            activebackground='#4b4e50',
            activeforeground='white',
            relief=tk.FLAT,
            pady=5
        )
        self.reset_btn.pack(side=tk.BOTTOM, fill=tk.X)  # 放在底部并水平铺满

        # -- 清空已采集按钮
        self.reset_collect_btn = tk.Button(
            root,
            text="重置所有已采集",
            command=self.reset_picking_data,
            bg='#4a2b2b',  # 深红色背景提示风险
            fg='white',
            activebackground='#6e3a3a',
            activeforeground='white',
            pady=5
        )
        # 放在自动采集开关下方，全图预览上方
        self.reset_collect_btn.pack(side=tk.BOTTOM, fill=tk.X)

        # -- 自动采集按钮
        self.auto_collect_var = tk.BooleanVar(value=False)
        self.auto_collect_cb = tk.Checkbutton(
            root,
            text="开启图标自动标记",
            variable=self.auto_collect_var,
            bg='#2b2b2b',
            fg='white',
            selectcolor='#3c3f41',
            activebackground='#2b2b2b',
            activeforeground='white'
        )
        self.auto_collect_cb.pack(side=tk.BOTTOM, fill=tk.X)

        # -- 开启最近路线规划
        self.auto_route_planning_var = tk.BooleanVar(value=False)
        self.auto_route_planning_cb = tk.Checkbutton(
            root,
            text="开启最近路线规划(需开启图标自动标记)",
            variable=self.auto_route_planning_var,
            bg='#2b2b2b',
            fg='white',
            selectcolor='#3c3f41',
            activebackground='#2b2b2b',
            activeforeground='white'
        )
        self.auto_route_planning_cb.pack(side=tk.BOTTOM, fill=tk.X)

        # -- 下拉选择资源类型
        log_step("正在加载下拉菜单")
        self.resource_type_options = []
        self.resource_type_selected_items = []
        self.resource_type_vars = {}
        self.resource_type_popup = None
        self.resource_type_text = "请选择"
        self.resource_type_button = tk.Button(root, text=self.resource_type_text, relief="groove",
                                bg="white", anchor="w", command=self.resource_type_toggle_popup)
        self.resource_type_button.pack(fill="x", expand=True)


        # -- 大地图按钮
        log_step("正在加载大地图按钮")
        self.big_map_btn = tk.Button(
            root, text="打开大地图预览", command=self.open_big_map,
            bg='#3c3f41', fg='white', pady=5
        )
        self.big_map_btn.pack(side=tk.BOTTOM, fill=tk.X)


        # 多线程初始化
        log_step("尝试初始化多线程")
        self.frame_queue = queue.Queue(maxsize=1) # 队列初始化
        self.is_running = True

        self.current_pos = (None, None)  # 存储计算线程算出的最新坐标

        import collections
        self.pos_history_x = collections.deque(maxlen=5)  # 保留最近5帧
        self.pos_history_y = collections.deque(maxlen=5)



        # 启动截图线程
        log_step("尝试启动截图线程")
        self.capture_thread = threading.Thread(target=self.capture_loop, daemon=True)
        self.capture_thread.start()

        # 启动匹配线程
        log_step("尝试启动匹配线程")
        self.match_thread = threading.Thread(target=self.match_loop, daemon=True)
        self.match_thread.start()

        # 其他参数
        self.consecutive_failures = 0  # 连续失败计数
        self.global_search_threshold = 10  # 超过10次失败就全球搜索
        self.found = False
        self.canvas_icons = {}  # 记录标记点对应的 Canvas ID
        self.bg_image_id = None  # 记录底图的 Canvas ID
        # -- UI平滑移动参数
        self.smooth_x = None
        self.smooth_y = None
        self.lerp_factor = 0.45

        # -- 拖动性能优化
        self.is_dragging = False
        self.drag_timer = None

        # 绑定窗口改变事件
        self.root.bind("<Configure>", self.on_window_configure)

        log_step("尝试启动update_tracker方法")
        self.update_tracker()

    def ui_delayed_init(self):
        # --- 状态记忆初始化 (惯性导航兜底) ---
        self.last_x = None
        self.last_y = None
        self.lost_frames = 0
        # --- 使用配置文件中的最大丢失帧数 ---
        self.MAX_LOST_FRAMES = config.MAX_LOST_FRAMES

        # --- 加载地图文件 ---
        log_step(f"正在加载地图 ({config.ORB_MAP_PATH})，请稍候...")
        self.logic_map_bgr = cv2.imread(config.ORB_MAP_PATH)
        log_step(f"正在加载地图 ({config.ORB_MAP_NOEDGE_PATH})，请稍候...")
        self.noedge_map_bgr = cv2.imread(config.ORB_MAP_NOEDGE_PATH)
        if self.logic_map_bgr is None or self.noedge_map_bgr is None:
            raise FileNotFoundError(f"找不到地图文件: {config.ORB_MAP_PATH}，请检查路径！")
        self.map_height, self.map_width = self.logic_map_bgr.shape[:2]

        self.enhanced_img = super_enhance(self.noedge_map_bgr)

        # --- 初始化 ORB 算法 ---
        log_step("正在提取地图的全局特征点...")
        # 注意：大地图需要非常多的特征点，保留上限。你可以根据实际地图大小调整 50000。
        self.orb = cv2.ORB_create(
            nfeatures=config.ORB_NFEATURES,
            scaleFactor=config.ORB_SCALEFACTOR,
            nlevels=config.ORB_NLEVELS,
            edgeThreshold=config.ORB_EDGETHRESHOLD,
            fastThreshold=config.ORB_FASTTHRESHOLD,
            firstLevel=0
        )
        self.orb_mini = cv2.ORB_create(
            nfeatures=config.ORB_MINI_NFEATURES,
            fastThreshold=2,
            edgeThreshold=1
        )


        if MATCHTYPE == "BF":
            self.bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)  # BF初始化
        elif MATCHTYPE == "FLANN":
            index_params = dict(
                algorithm=6,
                table_number=6,
                key_size=12,
                multi_probe_level=1
            )
            search_params = dict(checks=50)
            self.flann = cv2.FlannBasedMatcher(index_params, search_params)

        self.clahe = cv2.createCLAHE(
            clipLimit=getattr(config, 'ORB_CLAHE_LIMIT', config.ORB_CLIPLIMIT),
            tileGridSize=(8, 8)
        )

        # 构建多分辨率特征池
        log_step("正在构建多分辨率特征池 (多尺度预加载)...")

        self.init_big_map_features()

        # 预先提取大地图所有特征点的坐标数组 (避免每帧重新生成)
        self.pts_big_np = np.array([k.pt for k in self.kp_big], dtype=np.float32)

        # 调试用输出处理图片
        if DEBUG_MODE:
            cv2.imwrite("debug_big_map_features.png", self.logic_map_bgr)
            test_map = cv2.drawKeypoints(self.enhanced_img, self.kp_big, None, color=(0, 255, 0))
            cv2.imwrite("debug_big_map_enhanced.png", test_map)
            log_step(f"大地图特征点总数: {len(self.kp_big)}")

        # 加载资源点位数据
        # --- 提取所有唯一的资源类型
        self.resource_type_options = list(resource_type_dicts.keys())
        # 默认全选
        self.resource_type_selected_items = self.resource_type_options.copy()
        self.resource_type_button.config(text=f"过滤资源: 已选 {len(self.resource_type_options)} 类")

        self.marker_data = self.load_markers(config.POINTS_PATH)
        # 加载图标并缓存（包含灰色版本）
        self.icon_cache = self.prep_icons(r"assest/icons")
        log_step(f"已加载图标并缓存")

        # 屏幕截图设置 (MSS)
        self.sct = mss.mss()
        # --- 预先生成小地图的掩模 (Mask)
        mask_h = config.MINIMAP.get("height", 256)  # 替换为你的真实高度
        mask_w = config.MINIMAP.get("width", 256)  # 替换为你的真实宽度
        self.minimap_mask = np.zeros((mask_h, mask_w), dtype=np.uint8)
        cv2.circle(self.minimap_mask, (mask_w // 2, mask_h // 2), (mask_w // 2) - 5, 255, -1)
        # --- 预先定义小地图的中心点坐标 (用于后面的透视变换计算)
        self.mini_center_pt = np.float32([[[mask_w / 2, mask_h / 2]]])
        log_step(f"已获取小地图范围Mask")

        self.status_label.destroy()

    def run_selector_if_needed(self,force=False):
        """
        检查是否需要运行小地图校准工具。
        :param force: 如果为 True，无视配置强制重新校准
        """
        # 检查 config.json 中是否已经有了合法的坐标
        minimap_cfg = config.MINIMAP
        log_step(f"尝试加载minimap_cfg：{minimap_cfg}")
        has_valid_config = (
                'top' in minimap_cfg and
                'left' in minimap_cfg and
                'width' in minimap_cfg and
                'height' in minimap_cfg
        )

        if not has_valid_config or force:
            log_step("未检测到有效的小地图坐标，或请求重新校准。")
            try:
                # 等待 selector 窗口关闭后，才会继续执行下面的代码
                log_step(">>> 正在启动小地图选择器...")
                MinimapSelector(self.root)
                log_step("<<< 选择器关闭，坐标已更新！")
                return
            except Exception as e:
                log_step(f"小地图选择器发生错误：{e}")
                sys.exit(1)  # 如果连选择器都没有，且没有配置，只能退出程序

    def update_tracker(self):
        if not hasattr(self, 'marker_data'):
            self.root.after(100, self.update_tracker)
            return

        try:
            found = False
            need_save = False
            target_x, target_y = self.current_pos
            if target_x is not None:
                found = True

                # 如果是第一次定位，直接同步
                if self.smooth_x is None:
                    self.smooth_x, self.smooth_y = float(target_x), float(target_y)
                else:
                    # 2. 距离检查：如果瞬移距离过大（比如传送了），直接闪现过去，不进行平滑
                    dist_sq = (target_x - self.smooth_x) ** 2 + (target_y - self.smooth_y) ** 2
                    if dist_sq > 500 ** 2:
                        self.smooth_x, self.smooth_y = float(target_x), float(target_y)
                    else:
                        # 3. 指数平滑公式：New = Current + (Target - Current) * Factor
                        self.smooth_x += (target_x - self.smooth_x) * self.lerp_factor
                        self.smooth_y += (target_y - self.smooth_y) * self.lerp_factor

            # 使用平滑后的坐标进行后续的裁剪和渲染
            if self.smooth_x is not None:
                center_x, center_y = int(self.smooth_x), int(self.smooth_y)
                if found:
                    # 隐藏提示文字
                    if self.status_text_id:
                        self.canvas.itemconfig(self.status_text_id, state="hidden")

                    half_view = config.VIEW_SIZE // 2
                    x1, y1 = center_x - half_view, center_y - half_view
                    x2, y2 = center_x + half_view, center_y + half_view

                    # 大地地图边缘越界情况处理
                    bg_canvas = np.zeros((config.VIEW_SIZE, config.VIEW_SIZE, 3), dtype=np.uint8)
                    bg_canvas[:] = (43, 43, 43)

                    # 计算在原图上截取的合法范围
                    map_x1, map_y1 = max(0, x1), max(0, y1)
                    map_x2, map_y2 = min(self.map_width, x2), min(self.map_height, y2)

                    # 只有当截取范围有效时才进行像素复制
                    if map_x1 < map_x2 and map_y1 < map_y2:
                        # 计算在固定底板(bg_canvas)上的粘贴范围 (自动处理负坐标导致的偏移量)
                        paste_x1 = map_x1 - x1
                        paste_y1 = map_y1 - y1
                        paste_x2 = paste_x1 + (map_x2 - map_x1)
                        paste_y2 = paste_y1 + (map_y2 - map_y1)

                        # 将合法部分的地图贴到底板上，确保坐标系绝对对齐
                        bg_canvas[paste_y1:paste_y2, paste_x1:paste_x2] = self.logic_map_bgr[map_y1:map_y2, map_x1:map_x2]

                    # 将拼合好的底板转换为图片
                    pil_bg = Image.fromarray(cv2.cvtColor(bg_canvas, cv2.COLOR_BGR2RGB))
                    self.tk_bg_image = ImageTk.PhotoImage(pil_bg)

                    # 更新底层背景图片（如果不存在则创建）
                    if not hasattr(self, 'bg_image_id') or self.bg_image_id is None:
                        self.bg_image_id = self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_bg_image)
                        self.canvas.tag_lower(self.bg_image_id)  # 确保底图永远在最下层
                    else:
                        self.canvas.itemconfig(self.bg_image_id, image=self.tk_bg_image, state="normal")

                    # 独立管理图标 (不修改底图像素)
                    if not hasattr(self, 'canvas_icons'):
                        self.canvas_icons = {}

                    # 遍历所有标记，决定移动、显示还是隐藏
                    for m in self.marker_data:
                        m_id = m['id']
                        m_type = m['type']

                        # 安全检测
                        try:
                            m_type_int = int(m_type)
                        except ValueError:
                            continue  # 如果还是遇到了非数字，安全跳过该点，不要引发系统崩溃

                        # 判断该点位所属的分类是否被选中
                        is_visible = False
                        for category_name in self.resource_type_selected_items:
                            # 获取该分类对应的 ID 范围
                            range_min, range_max = resource_type_dicts.get(category_name, (0, 0))
                            if range_min <= m_type_int <= range_max:
                                is_visible = True
                                break

                        # 如果没被选中，直接隐藏并跳过
                        if not is_visible:
                            if m_id in self.canvas_icons:
                                self.canvas.itemconfig(self.canvas_icons[m_id], state="hidden")
                            continue

                        # 视锥剔除与距离计算
                        if x1 <= m['pixel_x'] <= x2 and y1 <= m['pixel_y'] <= y2:
                            dist = ((m['pixel_x'] - center_x)**2 + (m['pixel_y'] - center_y)**2)**0.5

                            if dist > 250:
                                # 距离过远，如果在画布上则隐藏
                                if m_id in self.canvas_icons:
                                    self.canvas.itemconfig(self.canvas_icons[m_id], state="hidden")
                                continue

                            # 自动采集逻辑
                            if self.auto_collect_var.get() and dist < config.PICKING_RADIUS and not m['is_collected']:
                                m['is_collected'] = True
                                need_save = True
                                if DEBUG_MODE:
                                    log_step(f"DEBUG: 自动采集资源点 {m['id']} (类型: {m['type']})")

                            # 计算在 Canvas 上的相对坐标
                            rx, ry = int(m['pixel_x'] - x1), int(m['pixel_y'] - y1)

                            icon_set = self.icon_cache.get(m['type'])
                            if not icon_set: continue

                            target_img = icon_set["tk_gray"] if m['is_collected'] else icon_set["tk_normal"]

                            # 如果 Canvas 上还没这个图标，创建它
                            if m_id not in self.canvas_icons:
                                item_id = self.canvas.create_image(rx, ry, anchor=tk.CENTER, image=target_img)
                                self.canvas_icons[m_id] = item_id
                            else:
                                # 如果已存在，仅更新位置、图片和状态（恢复显示）
                                item_id = self.canvas_icons[m_id]
                                self.canvas.coords(item_id, rx, ry)
                                self.canvas.itemconfig(item_id, image=target_img, state="normal")
                        else:
                            # 视野外，如果有对应的 item 则隐藏
                            if m_id in self.canvas_icons:
                                self.canvas.itemconfig(self.canvas_icons[m_id], state="hidden")

                    # 绘制玩家位置圆圈
                    view_w = config.VIEW_SIZE
                    view_h = config.VIEW_SIZE
                    center_x = view_w // 2
                    center_y = view_h // 2
                    radius = 8  # 圆圈半径

                    bbox = [center_x - radius, center_y - radius,
                            center_x + radius, center_y + radius]

                    radius_picking = radius + config.PICKING_RADIUS
                    bbox_picking = [center_x - radius_picking, center_y - radius_picking,
                            center_x + radius_picking, center_y + radius_picking]
                    # 清除渲染堆叠
                    self.canvas.delete("player_indicator")
                    self.canvas.delete("route_line") #旧路线

                    # 路线规划绘制逻辑
                    if getattr(self, 'auto_route_planning_var', None) and self.auto_route_planning_var.get() and self.found:
                        route_markers = self.calculate_collection_route(self.smooth_x, self.smooth_y, num_points=10)
                        if route_markers:
                            # 路线的起点是屏幕中心的玩家位置
                            prev_x, prev_y = center_x, center_y

                            for idx, m in enumerate(route_markers):
                                # 将大地图绝对坐标转换为 Canvas 相对坐标
                                rx, ry = int(m['pixel_x'] - x1), int(m['pixel_y'] - y1)

                                # 绘制连线 (带箭头，青色虚线)
                                self.canvas.create_line(
                                    prev_x, prev_y, rx, ry,
                                    fill="#00FFCC", width=2, dash=(4, 2), arrow=tk.LAST, tags="route_line"
                                )
                                # 绘制路线序号文字，增加阴影以保证在复杂底图上的可读性
                                self.canvas.create_text(
                                    rx + 12, ry - 12,
                                    text=str(idx + 1), fill="black", font=("微软雅黑", 10, "bold"), tags="route_line"
                                )
                                self.canvas.create_text(
                                    rx + 11, ry - 13,
                                    text=str(idx + 1), fill="#00FFCC", font=("微软雅黑", 10, "bold"), tags="route_line"
                                )

                                # 迭代下一个起点
                                prev_x, prev_y = rx, ry

                    # 采集范围圈
                    self.canvas.create_oval(bbox_picking, outline="yellow",dash=(4,2), width=1, tags="player_indicator")
                    if self.found:
                        # 定位成功：画红圈
                        self.canvas.create_oval(bbox, outline="red", width=2, tags="player_indicator")
                    else:
                        # 定位丢失：画白圈
                        self.canvas.create_oval(bbox, outline="white", width=2, tags="player_indicator")
            else:
                # 隐藏地图底图和所有图标
                if hasattr(self, 'bg_image_id') and self.bg_image_id:
                    self.canvas.itemconfig(self.bg_image_id, state="hidden")

                # 隐藏所有动态图标 (利用 tags 批量操作)
                self.canvas.itemconfigure("all", state="hidden")

                # 显示或更新提示文字
                self.empty_display_text = "计算匹配定位锚点中...\n请勿用任何窗口遮挡小地图，包括本软件！\n建议全屏游戏以保证小地图显示最大\n\n在此页面卡住超过10分钟\n是win显示设置缩放不为100%导致的"
                center_pt = config.VIEW_SIZE // 2

                if not self.status_text_id:
                    # 第一次创建：白色文字，带一点点阴影效果（创建两个 text）
                    self.status_text_id = self.canvas.create_text(
                        center_pt, center_pt,
                        text=self.empty_display_text,
                        fill="white",
                        font=("微软雅黑", 14, "bold"),
                        justify=tk.CENTER,
                        tags="status_msg"
                    )

                else:
                    self.canvas.itemconfig(self.status_text_id, state="normal", text=self.empty_display_text)
                    self.canvas.tag_raise(self.status_text_id)

            if need_save and int(time.time() * 10) % 10 == 0:
                self.save_picking_data(config.PICKINGDATA_PATH)


        except Exception as e:
            if DEBUG_MODE:
                log_step(f"UI 刷新线程发生异常: {e}")


        finally:
            # --- 使用配置文件中的刷新频率 ---
            if self.is_dragging:
                # 正在拖动时，只维持 100ms 一次的低频检查，或者直接 return
                self.root.after(100, self.update_tracker)
                return
            else:
                self.root.after(config.ORB_REFRESH_RATE, self.update_tracker)

    def build_multi_scale_feature_pool(self):
        """对大地图进行多尺度缩放并提取特征合并"""
        logic_gray_raw = self.enhanced_img #cv2.cvtColor(self.enhanced_img, cv2.COLOR_BGR2GRAY)

        self.map_height, self.map_width = logic_gray_raw.shape[:2]

        # 定义缩放层级：0.8倍（更远）, 1.0倍（原始）, 1.2倍（更近）
        # 如果你的游戏缩放变化很大，可以增加更多层级如 [0.6, 0.8, 1.0, 1.2, 1.4]
        scales = [0.6, 0.8, 1.0, 1.2, 1.4]

        all_kp = []
        all_des = []

        for s in scales:
            log_step(f"  -> 正在提取 {s}x 缩放层级的特征...")
            if s == 1.0:
                layer_gray = logic_gray_raw
            else:
                # 缩放图片
                w = int(self.map_width * s)
                h = int(self.map_height * s)
                layer_gray = cv2.resize(logic_gray_raw, (w, h), interpolation=cv2.INTER_LINEAR)

            kp, des = self.extract_grid_features(
                layer_gray,
                total_features = config.MAX_KP_PER_LAYER,
                grid_rows = int(config.ORB_GRID[0]*s),
                grid_cols = int(config.ORB_GRID[1]*s))

            if des is not None:
                # 【关键】坐标还原：将缩放后的坐标还原回原始大地图坐标系
                for k in kp:
                    k.pt = (k.pt[0] / s, k.pt[1] / s)

                all_kp.extend(kp)
                all_des.append(des)


        # 合并所有层级的描述子
        self.kp_big = all_kp
        self.des_big = np.vstack(all_des)

        log_step(f"特征池构建完成，总计特征点: {len(self.kp_big)}")

    def extract_grid_features(self, image_gray, total_features=100000, grid_rows=30, grid_cols=30):
        """
        分块提取特征点，确保全图均匀分布
        """
        h, w = image_gray.shape
        dy, dx = h // grid_rows, w // grid_cols
        # 计算每个小格子里应该有多少个点
        features_per_grid = total_features // (grid_rows * grid_cols)

        # 初始化一个针对小块提取的 ORB 实例
        # 这里的 fastThreshold 必须调低，否则草地块可能一个点都抓不到
        grid_orb = cv2.ORB_create(
            nfeatures=features_per_grid,
            fastThreshold=2,
            edgeThreshold=1,
        )

        all_kp = []
        all_des = []

        for i in range(grid_rows):
            for j in range(grid_cols):
                # 1. 确定当前小格子的坐标范围
                y1, y2 = i * dy, (i + 1) * dy
                x1, x2 = j * dx, (j + 1) * dx
                roi = image_gray[y1:y2, x1:x2]

                # 2. 在小格子里提取特征
                kp, des = grid_orb.detectAndCompute(roi, None)

                if des is not None:
                    # 3. 【关键】坐标还原：将局部坐标加上偏移量，还原回大地图坐标
                    for k in kp:
                        k.pt = (k.pt[0] + x1, k.pt[1] + y1)

                    all_kp.extend(kp)
                    all_des.append(des)

        # 合并所有描述子
        if all_des:
            final_des = np.vstack(all_des)
            return all_kp, final_des
        return [], None

    def calculate_collection_route(self, start_x, start_y, num_points=10):
        """
        计算从指定坐标开始的连续最近采集路线
        """
        # 1. 获取当前需要显示的、未采集的可用资源点
        valid_markers = []
        for m in self.marker_data:
            if m.get('is_collected'):
                continue

            try:
                m_type_int = int(m['type'])
            except ValueError:
                continue  # 如果还是遇到了非数字，安全跳过该点，不要引发系统崩溃

            # 检查该点位所属的分类是否被选中
            is_visible = False
            for category_name in self.resource_type_selected_items:
                range_min, range_max = resource_type_dicts.get(category_name, (0, 0))
                if range_min <= m_type_int <= range_max:
                    is_visible = True
                    break

            if is_visible:
                valid_markers.append(m)

        if not valid_markers:
            return []

        route = []
        current_x, current_y = start_x, start_y
        candidates = valid_markers.copy()

        # 2. 贪心算法：每次找距离当前坐标最近的下一个点
        for _ in range(min(num_points, len(candidates))):
            nearest_m = None
            min_dist_sq = float('inf')

            for m in candidates:
                dist_sq = (m['pixel_x'] - current_x) ** 2 + (m['pixel_y'] - current_y) ** 2
                if dist_sq < min_dist_sq:
                    min_dist_sq = dist_sq
                    nearest_m = m

            if nearest_m:
                route.append(nearest_m)
                candidates.remove(nearest_m)
                # 更新当前坐标为刚找到的资源点，以便寻找下一段路线
                current_x, current_y = nearest_m['pixel_x'], nearest_m['pixel_y']

        return route

    def load_picking_data(self,json_path):
        """从 json 加载已采集的 ID 列表"""
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    return set(json.load(f))  # 使用 set 提高查询效率
            except:
                return set()
        return set()

    def save_picking_data(self,json_path):
        """将当前已采集的 ID 存入 json"""
        collected_ids = [m['id'] for m in self.marker_data if m['is_collected']]
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(collected_ids, f, ensure_ascii=False, indent=4)

    def load_progress(self):
        """从本地 JSON 加载已采集的点位 ID 列表"""
        progress_file = "user_progress.json"
        if os.path.exists(progress_file):
            try:
                with open(progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("collected_ids", [])
            except:
                return []
        return []

    def save_progress(self):
        """将当前内存中的采集状态保存到本地"""
        progress_file = "user_progress.json"
        # 提取所有 is_collected 为 True 的点位 ID
        collected_ids = [m['id'] for m in self.marker_data if m.get('is_collected')]
        # 提取所有自定义点位
        custom_markers = [m for m in self.marker_data if m.get('is_custom')]

        with open(progress_file, 'w', encoding='utf-8') as f:
            json.dump({
                "collected_ids": collected_ids,
                "custom_markers": custom_markers
            }, f, ensure_ascii=False, indent=4)

    def load_markers(self, json_path):
        """加载资源点并转换坐标"""
        # --- 这里的参数必须和拼接大图时的参数完全一致 ---
        X_MIN = -12
        Y_MIN = -11
        TILE_SIZE = 256
        SCALE = 1
        # --------------------------------------------
        collected_ids = self.load_picking_data(config.PICKINGDATA_PATH)  # 获取已采集列表
        processed_markers = []
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 假设 JSON 结构是包含 'points' 列表的
                points_dict = data if isinstance(data, dict) else {}

                for item in self.resource_type_selected_items:
                    type_range = resource_type_dicts[item]
                    for type_str,value_list in points_dict.items():
                        if value_list is None or not isinstance(value_list, list):
                            continue

                        try:
                            type_int = int(type_str)
                        except ValueError:
                            continue  # 跳过非数字的 Key

                        if type_range[0] <= type_int <= type_range[1]:
                            for point in value_list:
                                # 获取坐标（增加 get 容错）
                                pt = point.get('point', {})
                                lat = pt.get('lat')
                                lng = pt.get('lng')

                                if lat is None or lng is None:
                                    continue

                                m_id = point.get('id')
                                m_type_raw = point.get('markType')
                                if m_type_raw is None or not str(m_type_raw).isdigit():
                                    continue

                                # 核心换算公式
                                px = int((lng / TILE_SIZE - X_MIN) * TILE_SIZE * SCALE)
                                py = int((lat / TILE_SIZE - Y_MIN) * TILE_SIZE * SCALE)

                                processed_markers.append({
                                    'id': m_id,
                                    'type': str(m_type_raw),
                                    'pixel_x': px,
                                    'pixel_y': py,
                                    'is_collected': m_id in collected_ids
                                })
            log_step(f"成功加载 {len(processed_markers)} 个资源点")
        except Exception as e:
            log_step(f"加载点位失败: {e}")

        return processed_markers

    def prep_icons(self, icon_dir):
        """预加载图标并生成灰度版本"""
        cache = {}
        if not os.path.exists(icon_dir):
            log_step(f"警告: 找不到图标目录 {icon_dir}")
            return cache

        for fname in os.listdir(icon_dir):
            if fname.endswith(".png"):
                m_type = fname.split(".")[0]
                try:
                    # 1. 加载并缩放原始图标
                    img = Image.open(os.path.join(icon_dir, fname)).convert("RGBA")
                    img = img.resize((24, 24), Image.Resampling.LANCZOS)

                    # 2. 生成灰色版本 (使用更加现代的方法避开 getdata 警告)
                    # 将 RGB 转为 L (灰度)，再转回 RGBA
                    gray_img = img.convert("L").convert("RGBA")

                    # 3. 处理透明度：获取原始图标的 alpha 通道并减半
                    r, g, b, alpha = img.split()
                    # 使用 point 函数批量处理像素，0.5 表示 50% 的不透明度
                    half_alpha = alpha.point(lambda p: p * 0.5)

                    # 将减半后的透明度合并回灰色图标
                    gray_img.putalpha(half_alpha)

                    cache[m_type] = {
                        "pil_normal": img,
                        "pil_gray": gray_img,
                        "tk_normal": ImageTk.PhotoImage(img),
                        "tk_gray": ImageTk.PhotoImage(gray_img)
                    }
                except Exception as e:
                    log_step(f"图标 {fname} 加载失败: {e}")
        return cache

    def capture_loop(self):
        """专门负责截屏的生产者线程"""
        with mss.mss() as sct:
            while self.is_running:
                if not hasattr(self, 'minimap_region') or self.minimap_region is None:
                    time.sleep(0.1)
                    continue
                try:
                    # 截图
                    screenshot = sct.grab(self.minimap_region)
                    minimap_bgr = np.array(screenshot)

                    # 如果图片极大面积是纯黑，说明游戏屏蔽了截图或全屏了
                    if np.mean(minimap_bgr) < 5:
                        log_step(
                            "警告: 捕获到的画面几乎为纯黑！请尝试使用【窗口化/无边框】运行游戏，或以管理员身份运行本程序。")

                    # 图像增强/灰度化 (把这一步放在截图线程，分担主计算线程的压力)
                    gray = super_enhance(minimap_bgr, isPlayer=False)

                    # 调试用输出处理图片
                    if DEBUG_MODE:
                        cv2.imwrite("debug_mini_map_bgr.png", minimap_bgr)
                        cv2.imwrite("debug_mini_map_enhanced.png", gray)

                    # 压入队列，保持最新帧
                    if self.frame_queue.full():
                        try:
                            self.frame_queue.get_nowait()  # 如果队列满了，丢弃旧帧
                        except queue.Empty:
                            pass
                    self.frame_queue.put(gray)

                    # 核心降温：限制截图帧率
                    # 0.033 秒约等于 30 FPS
                    time.sleep(0.033)

                except Exception as e:
                    log_step(f"截图线程发生错误: {e}")
                    time.sleep(1)

    def match_loop(self):
        """专门负责计算的消费者线程 - 读写分离版"""
        while self.is_running:
            if not hasattr(self, 'orb_mini') or not hasattr(self, 'kp_big'):
                time.sleep(0.5)
                continue
            if not hasattr(self, 'minimap_mask') or not hasattr(self, 'minimap_mask'):
                time.sleep(0.5)
                continue
            try:
                # 1. 从队列获取最新帧 (如果队列为空，会在这里阻塞等待，不占 CPU)
                # 设置 timeout 防止线程死锁无法退出
                try:
                    gray = self.frame_queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                # 2. 提取特征 (直接使用传入的 gray 和预计算好的 mask)
                kp_mini, des_mini = self.orb_mini.detectAndCompute(gray, self.minimap_mask)

                # ... 这里完全保留上一回合为你优化的逻辑，不作任何删减 ...
                # ★ 优化：构建搜索计划列表，同时尝试局部和全局搜索
                search_plans = []
                is_global_mode = (self.last_x is None) or (
                            self.consecutive_failures >= self.global_search_threshold)

                if not is_global_mode and self.last_x is not None:
                    # 局部搜索
                    dist_sq = (self.pts_big_np[:, 0] - self.last_x) ** 2 + (
                                self.pts_big_np[:, 1] - self.last_y) ** 2
                    search_radius = 800 + (self.consecutive_failures * 200)
                    near_indices = np.where(dist_sq < search_radius ** 2)[0]

                    if len(near_indices) > 20:
                        local_des = self.des_big[near_indices]
                        local_kp = [self.kp_big[i] for i in near_indices]
                        search_plans.append((local_des, local_kp, "local"))

                # ★ 始终添加全局搜索作为备选
                search_plans.append((self.des_big, self.kp_big, "global"))

                # ★ 循环尝试所有搜索计划
                found = False
                raw_x, raw_y = None, None
                search_type_used = None

                for target_des, target_kp, search_type in search_plans:
                    # 执行匹配
                    if MATCHTYPE == "BF":
                        matches = self.bf.knnMatch(des_mini, target_des, k=2)
                    elif MATCHTYPE == "FLANN":
                        matches = self.flann.knnMatch(des_mini, target_des, k=2)

                    good_matches = []
                    ratio_thresh = getattr(config, 'ORB_RATIO', config.ORB_RATIO)

                    for match_pair in matches:
                        if len(match_pair) == 2:
                            m, n = match_pair
                            if m.distance < ratio_thresh * n.distance:
                                good_matches.append(m)
                        elif len(match_pair) == 1:
                            good_matches.append(match_pair[0])

                    good_matches = sorted(good_matches, key=lambda x: x.distance)[:100]

                    if len(good_matches) >= config.ORB_MIN_MATCH_COUNT:
                        src_pts = np.float32([kp_mini[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
                        dst_pts = np.float32([target_kp[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

                        M, inliers = cv2.estimateAffinePartial2D(
                            src_pts, dst_pts,
                            method=cv2.RANSAC,
                            ransacReprojThreshold=1.5,
                            maxIters=2000
                        )

                        if M is not None:
                            s = np.sqrt(M[0, 0] ** 2 + M[0, 1] ** 2)

                            if 0.6 <= s <= 1.4:
                                center_h = np.array([[[config.MINIMAP.get("width") / 2, config.MINIMAP.get("height") / 2]]], dtype=np.float32)
                                dst = cv2.transform(center_h, M)
                                raw_x, raw_y = int(dst[0][0][0]), int(dst[0][0][1])

                                # ★ 关键区别：只有局部搜索才做距离校验
                                if search_type == "local" and self.last_x is not None:
                                    dist = np.sqrt((raw_x - self.last_x) ** 2 + (raw_y - self.last_y) ** 2)
                                    if dist > 200:
                                        # 局部结果太远，跳过这个结果，继续尝试全局
                                        continue

                                # 找到有效结果
                                found = True
                                search_type_used = search_type
                                break
                    else:
                        if search_type == "local":
                            # 局部搜索匹配点不足，跳过并尝试全局
                            continue

                # ★ 统一处理结果
                if found and raw_x is not None and (0 <= raw_x <= self.map_width) and (0 <= raw_y <= self.map_height):
                    self.pos_history_x.append(raw_x)
                    self.pos_history_y.append(raw_y)

                    median_x = int(np.median(self.pos_history_x))
                    median_y = int(np.median(self.pos_history_y))

                    self.last_x, self.last_y = median_x, median_y
                    self.current_pos = (median_x, median_y)
                    if search_type_used == "global":
                        self.consecutive_failures = 0
                    self.found = True
                else:
                    self.consecutive_failures += 1
                    self.found = False

            except Exception as e:
                log_step(f"匹配线程发生错误: {e}")
                time.sleep(1)

            # 缓解 GIL 锁争夺
            time.sleep(0.03)

    def reset_location(self):
        """手动清空状态，强制进入全局匹配模式"""
        # 清空滤波队列
        if hasattr(self, 'pos_history_x'):
            self.pos_history_x.clear()
            self.pos_history_y.clear()

        self.last_x = None
        self.last_y = None
        self.current_pos = (None, None)

        # 如果你参考上个回复添加了平滑变量，也要在这里重置
        if hasattr(self, 'smooth_x'):
            self.smooth_x = None
            self.smooth_y = None

        # 核心：将失败计数直接设为阈值，诱导 match_loop 进入 is_global_mode
        self.consecutive_failures = self.global_search_threshold
        self.found = False

        log_step(">>> 已手动重置定位系统，正在尝试全图重新定位...")

    def open_big_map(self):
        # 确保 logic_map_bgr 已经转为 PIL 格式
        pil_full_map = Image.fromarray(cv2.cvtColor(self.logic_map_bgr, cv2.COLOR_BGR2RGB))
        # 打开新窗口
        BigMapWindow(self.root, pil_full_map, self.marker_data, self.icon_cache, self.resource_type_selected_items)

    def on_window_configure(self, event):
        # 增加类型判断
        if event.widget != self.root:
            return

        # 标记正在拖动
        self.is_dragging = True
        if DEBUG_MODE:
            log_step(">>> 窗口移动，暂停工作逻辑")

        # 如果已有计时器，取消它
        if self.drag_timer:
            self.root.after_cancel(self.drag_timer)

        # 400ms 后如果没有新的位移，认为拖动结束
        self.drag_timer = self.root.after(400, self.on_drag_end)

    def on_drag_end(self):
        self.is_dragging = False
        self.drag_timer = None
        if DEBUG_MODE:
            log_step(">>> 窗口停止移动，恢复工作逻辑")

    def reset_picking_data(self):
        """重置所有已采集标记"""

        # 弹出二次确认弹窗，防止误操作
        if not messagebox.askyesno("确认重置", "确定要清空所有已采集记录吗？此操作不可撤销。"):
            return

        try:
            # 1. 清空内存中的 ID 集合
            #self.collected_ids.clear()

            # 2. 修改 marker_data 中每个点位的状态
            for m in self.marker_data:
                m['is_collected'] = False

            # 3. 清空磁盘文件
            if os.path.exists(config.PICKINGDATA_PATH):
                with open(config.PICKINGDATA_PATH, 'w', encoding='utf-8') as f:
                    json.dump([], f)  # 写入空列表

            log_step(">>> 已成功重置所有采集标记。")

            # 4. 强制刷新一次 UI (如果有打开大地图，建议关闭大地图重开)
            # 这里主循环 update_tracker 会在下一帧自动应用这些改变

        except Exception as e:
            messagebox.showerror("重置失败", f"发生错误: {e}")

    def init_big_map_features(self):
        cache_file = config.FEATURES_PATH

        # 构建地图特征池提示
        self.status_label.config(text="正在构建地图特征池，请稍候...")
        self.root.update()  # 刷新文字

        # 检查缓存是否存在
        if os.path.exists(cache_file):
            try:
                self.kp_big, self.des_big = self.load_features(cache_file)
                # 别忘了更新我们上一回合优化的预计算坐标数组
                self.pts_big_np = np.array([k.pt for k in self.kp_big], dtype=np.float32)
                return
            except Exception as e:
                log_step(f"缓存读取失败，重新计算中... {e}")

        # 如果缓存不存在，则正常计算
        log_step("正在初次计算大地图特征点，请稍候...")

        self.status_label.config(text="正在构建地图特征池：计算 ORB 特征点 (耗时较长)...")
        self.root.update()  # 再次刷新

        self.build_multi_scale_feature_pool()

        # 计算完成后存入缓存
        self.save_features(cache_file, self.kp_big, self.des_big)

    def save_features(self,file_path, keypoints, descriptors):
        """将特征点和描述符保存到磁盘"""
        # 提取 KeyPoint 的核心属性：坐标(pt), 尺寸(size), 角度(angle), 响应强度(response), 层级(octave), ID(class_id)
        kp_array = np.array([(kp.pt[0], kp.pt[1], kp.size, kp.angle, kp.response, kp.octave, kp.class_id)
                             for kp in keypoints],
                            dtype=[('pt_x', 'f4'), ('pt_y', 'f4'), ('size', 'f4'), ('angle', 'f4'),
                                   ('response', 'f4'), ('octave', 'i4'), ('class_id', 'i4')])

        # 使用 savez_compressed 进行高比例压缩保存
        np.savez_compressed(file_path, keypoints=kp_array, descriptors=descriptors)
        log_step(f">>> 特征数据已缓存至: {file_path}")

    def load_features(self,file_path):
        """从磁盘读取并还原特征点和描述符"""
        data = np.load(file_path)
        kp_array = data['keypoints']
        descriptors = data['descriptors']

        # 还原为 cv2.KeyPoint 对象列表
        keypoints = [cv2.KeyPoint(x=row['pt_x'], y=row['pt_y'], size=row['size'], angle=row['angle'],
                                  response=row['response'], octave=row['octave'], class_id=row['class_id'])
                     for row in kp_array]

        log_step(f">>> 已从缓存加载 {len(keypoints)} 个特征点")
        return keypoints, descriptors

    def resource_type_toggle_popup(self):
        if self.resource_type_popup and self.resource_type_popup.winfo_exists():
            self.resource_type_close_popup()
            return

            # 创建一个无边框的置顶窗口
        self.resource_type_popup = tk.Toplevel(self.root)
        self.resource_type_popup.overrideredirect(True)  # 去掉标题栏
        self.resource_type_popup.attributes("-topmost", True)  # 确保下拉框也在最前面

        # 计算弹出位置（在按钮正下方）
        x = self.resource_type_button.winfo_rootx()
        y = self.resource_type_button.winfo_rooty() - 10 # 向上弹出，因为按钮在底部
        self.resource_type_popup.geometry(f"+{x}+{y}")

        # 绑定点击外部自动关闭
        self.resource_type_popup.bind("<FocusOut>", lambda e: self.resource_type_close_popup())

        # 创建容器并加滚动条
        frame = tk.Frame(self.resource_type_popup, bg="white", highlightbackground="gray", highlightthickness=1)
        frame.pack()

        for option in self.resource_type_options:
            if option not in self.resource_type_vars:
                self.resource_type_vars[option] = tk.BooleanVar(value=option in self.resource_type_selected_items)

            cb = tk.Checkbutton(frame, text=option, variable=self.resource_type_vars[option],
                                bg="white", anchor="w", padx=10,
                                command=self.resource_type_update_selection)
            cb.pack(fill="x")

        self.resource_type_popup.focus_set()

    def resource_type_update_selection(self):
        self.resource_type_selected_items = [opt for opt, var in self.resource_type_vars.items() if var.get()]
        if not self.resource_type_selected_items:
            self.resource_type_button.config(text="请选择")
        else:
            text = ", ".join(self.resource_type_selected_items)
            # 如果太长，显示数量
            if len(text) > 20:
                text = f"已选择 {len(self.resource_type_selected_items)} 项"
            self.resource_type_button.config(text=text)

    def resource_type_close_popup(self):
        if self.resource_type_popup:
            self.resource_type_popup.destroy()
            self.resource_type_popup = None

    def resource_type_get_value(self):
        """获取选中的列表"""
        return self.resource_type_selected_items

    def start_hotkey_listener(self):
        """使用 pynput 监听全局快捷键"""

        def on_activate():
            # 当按下 Alt+L 时触发
            # 注意：pynput 在独立线程运行，修改 UI 必须回到主线程
            log_step("检测到触发快捷键ALT+L")
            self.root.after(0, self.hotkey_triggered)

        # 注意：这里的键名必须是小写，组合用 + 号
        # <alt>+l 代表 Alt + L
        try:
            # 实例化 GlobalHotkeys
            listener = keyboard.GlobalHotKeys({
                '<alt>+l': on_activate
            })
            listener.daemon = True # 设置为守护线程，随主程序退出
            listener.start()
            log_step(">>> 快捷键监听已启动 (Alt+L)")
        except Exception as e:
            log_step(f">>> 快捷键启动失败: {e}")

    def hotkey_triggered(self):
        """快捷键触发后的逻辑"""
        new_state = not self.ui_lock_var.get()
        self.ui_lock_var.set(new_state)
        self.apply_ui_lock(new_state)

    def toggle_ui_lock_from_cb(self):
        """用户通过点击复选框触发"""
        self.apply_ui_lock(self.ui_lock_var.get())

    def apply_ui_lock(self, is_locked):
        """执行鼠标穿透逻辑"""
        if sys.platform != "win32":
            return

        try:
            # 获取窗口句柄
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            # 获取当前样式
            ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)

            if is_locked:
                # 添加透明穿透属性：WS_EX_TRANSPARENT 允许鼠标穿透
                # WS_EX_LAYERED 是必须的，否则穿透无效
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
                self.ui_lock_cb.config(fg="#00FF00") # 锁定状态变绿提醒
            else:
                # 移除穿透属性
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style & ~WS_EX_TRANSPARENT)
                self.ui_lock_cb.config(fg="white")
        except Exception as e:
            print(f"设置UI锁定失败: {e}")

class MinimapSelector(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("小地图校准器")

        # --- 窗口样式设置 ---
        self.overrideredirect(True)  # 去除系统窗口边框
        self.attributes("-topmost", True)  # 永远置顶
        self.attributes("-alpha", 0.5)  # 设置整体半透明(50%)，方便看透下方的游戏
        self.configure(bg='black')  # 背景纯黑

        # --- 初始化状态 ---
        self.size = 150
        self.x = 100
        self.y = 100

        # 从现有配置文件中读取上一次的位置
        self.load_initial_pos()

        # 设置初始位置和大小
        self.geometry(f"{self.size}x{self.size}+{self.x}+{self.y}")

        # --- 创建画布 ---
        self.canvas = tk.Canvas(self, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.draw_ui()

        # --- 绑定鼠标与键盘事件 ---
        self.canvas.bind("<ButtonPress-1>", self.on_press)  # 鼠标左键按下
        self.canvas.bind("<B1-Motion>", self.on_drag)  # 鼠标左键按住拖动

        # 绑定鼠标滚轮 (Windows)
        self.bind("<MouseWheel>", self.on_scroll)
        # 绑定鼠标滚轮 (Linux/Mac 兼容)
        self.bind("<Button-4>", lambda e: self.resize(10))
        self.bind("<Button-5>", lambda e: self.resize(-10))

        # 绑定回车键和双击保存
        self.bind("<Return>", self.save_and_exit)
        self.bind("<Double-Button-1>", self.save_and_exit)
        # 按 ESC 退出不保存
        self.bind("<Escape>", lambda e: self.destroy())

        log_step("等待用户选择小地图位置")

    def load_initial_pos(self):
        """尝试从 config.json 读取上次保存的坐标"""
        if os.path.exists(CONFIG_FILE):
            try:
                minimap = config.MINIMAP
                if minimap:
                    self.x = minimap.get("left", 100)
                    self.y = minimap.get("top", 100)
                    self.size = minimap.get("width", 150)
            except Exception:
                log_step(f"小地图框选器发生错误：{e}")

    def draw_ui(self):
        """绘制界面元素 (圆形准星和提示文字)"""
        self.canvas.delete("all")
        w = 3  # 边框厚度

        # 1. 绘制表示小地图边界的绿色圆圈
        self.canvas.create_oval(w, w, self.size - w, self.size - w, outline="#00FF00", width=w)

        # 2. 绘制十字准星中心辅助线
        self.canvas.create_line(0, self.size // 2, self.size, self.size // 2, fill="#00FF00", dash=(4, 4))
        self.canvas.create_line(self.size // 2, 0, self.size // 2, self.size, fill="#00FF00", dash=(4, 4))

        # 3. 绘制操作提示文字
        self.canvas.create_text(self.size // 2, 15, text="左键拖动 | 滚轮缩放\n圆框一定要比小地图的框要小", fill="white",
                                font=("Microsoft YaHei", 9, "bold"))
        self.canvas.create_text(self.size // 2, self.size - 15, text="按 回车/双击 保存", fill="yellow",
                                font=("Microsoft YaHei", 9, "bold"))

    def on_press(self, event):
        """记录鼠标按下的起始位置"""
        self.start_x = event.x
        self.start_y = event.y

    def on_drag(self, event):
        """计算鼠标拖动的偏移量并移动窗口"""
        dx = event.x - self.start_x
        dy = event.y - self.start_y
        self.x = self.winfo_x() + dx
        self.y = self.winfo_y() + dy
        self.geometry(f"{self.size}x{self.size}+{self.x}+{self.y}")

    def on_scroll(self, event):
        """处理鼠标滚轮放大缩小"""
        # Windows 的 delta 通常是 120 的倍数
        if event.delta > 0:
            self.resize(10)  # 向上滚放大
        else:
            self.resize(-10)  # 向下滚缩小

    def resize(self, delta):
        """改变窗口尺寸"""
        self.size += delta
        if self.size < 80:
            self.size = 80  # 限制最小不能低于 80 像素
        self.geometry(f"{self.size}x{self.size}+{self.x}+{self.y}")
        self.draw_ui()

    def save_and_exit(self, event=None):
        """完全解决 Tkinter 句柄失效、0x0 报错以及 DPI 缩放问题的终极保存逻辑"""
        import ctypes
        from ctypes import wintypes

        self.update_idletasks()

        # 使用 wm_frame() 获取 Windows 认可的顶级窗口真正句柄
        hwnd_str = self.wm_frame()
        try:
            # wm_frame() 返回的是十六进制字符串(如 '0x12345')，将其转换为 int 句柄
            if hwnd_str.startswith('0x'):
                hwnd = int(hwnd_str, 16)
            else:
                hwnd = int(hwnd_str)
        except Exception:
            hwnd = 0

        rect = wintypes.RECT()
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        success = False

        # 尝试通过 DWM 穿透获取视觉物理像素
        if hwnd != 0:
            try:
                result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                    ctypes.wintypes.HWND(hwnd),
                    ctypes.wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
                    ctypes.byref(rect),
                    ctypes.sizeof(rect)
                )
                # 返回值为 0 代表调用成功
                if result == 0 and (rect.right - rect.left) > 0:
                    real_left = rect.left
                    real_top = rect.top
                    real_width = rect.right - rect.left
                    real_height = rect.bottom - rect.top
                    success = True
                    log_step("🎯 [方案 A] 成功通过 DWM 获取真实物理像素。")
            except Exception as e:
                log_step(f"方案 A 异常: {e}")

        # 如果方案 A 失败或返回了 0，直接抓取 Windows 物理缩放比例进行硬计算
        if not success:
            log_step("⚠️ [方案 A] 失败，自动切换至 [方案 B] 物理缩放硬计算...")
            try:
                # 获取 Windows 的真实缩放 DPI (96 为 100%, 144 为 150%)
                hdc = ctypes.windll.user32.GetDC(0)
                dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # 88 代表 LOGPIXELSX
                ctypes.windll.user32.ReleaseDC(0, hdc)
                scale_factor = dpi / 96.0  # 算出缩放倍率，比如 1.25 或 1.5

                # 直接通过 Tkinter 逻辑坐标 * 缩放倍率 = 真实物理像素
                real_left = int(self.winfo_rootx() * scale_factor)
                real_top = int(self.winfo_rooty() * scale_factor)
                real_width = int(self.winfo_width() * scale_factor)
                real_height = int(self.winfo_height() * scale_factor)
            except Exception as e:
                # 最终无解情况下的暴力兜底
                log_step(f"方案 B 异常: {e}")
                real_left = self.winfo_rootx()
                real_top = self.winfo_rooty()
                real_width = self.winfo_width()
                real_height = self.winfo_height()

        # 2. 写入配置文件
        config_data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
            except Exception:
                pass

        config_data["MINIMAP"] = {
            "top": real_top,
            "left": real_left,
            "width": real_width,
            "height": real_height
        }

        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)

        log_step(f"✅ 坐标锁定成功! 物理位置: ({real_left}, {real_top}) 大小: {real_width}x{real_height}")
        self.destroy()

def log_step(step_name):
    step_info = f"[{time.strftime('%H:%M:%S')}] {step_name}\n"
    print(step_info)
    with open("log.txt", "a", encoding="utf-8") as f:
        f.write(step_info)

def run_bootstrapper(force_selector=True):
    root = tk.Tk()
    root.withdraw()

    # 检查配置是否存在
    log_step("正在检测是否需要小地图选择器")
    needs_selector = not config.MINIMAP or force_selector
    if needs_selector:
        log_step("正在创建选择器UI")
        # 2. 创建一个临时的 Toplevel 运行选择器
        selector_app = MinimapSelector(root)

        # --- 核心：阻塞逻辑 ---
        # 告诉 Tkinter，在这里停住，直到 selector_win 被销毁
        root.wait_window(selector_app)

        log_step("选择器检查完成")

        # 再次检查配置，如果用户直接关了没保存，就退出
        if not os.path.exists(CONFIG_FILE):
            messagebox.showwarning("提示", "未完成校准，程序将退出。")
            root.destroy()
            sys.exit()

    # 因为配置文件被 selector 修改了，我们需要重新加载一次 config 模块的数据
    import importlib
    importlib.reload(config)

    log_step("显示主窗口")
    root.deiconify()  # 重新显示主窗口
    app = MapTrackerApp(root)
    root.mainloop()

class ResourceDownload:
    def __init__(self):
        self.url_point = f"https://wiki.biligame.com/rocom/Data:Mapnew/point.json"



if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    log_step("程序启动")

    if MATCHTYPE not in ["FLANN","BF"]:
        MATCHTYPE = "BF"

    try:
        run_bootstrapper(force_selector=True)
    except Exception as e:
        # 捕捉所有导致程序崩溃的致命错误
        error_msg = traceback.format_exc()

        # 写入本地日志文件
        try:
            with open("crash_log.txt", "w", encoding="utf-8") as f:
                f.write(error_msg)
        except:
            pass  # 如果连写文件的权限都没有，就忽略

        # 弹窗显示错误给用户
        temp_root = tk.Tk()
        temp_root.withdraw()  # 隐藏主窗口
        temp_root.attributes("-topmost", True)
        log_step(f"程序发生严重错误已停止运行。\n\n错误信息已保存到 crash_log.txt\n\n详情:\n{str(e)}")
        tk.messagebox.showerror(
            "程序崩溃",
            f"程序发生严重错误已停止运行。\n\n错误信息已保存到 crash_log.txt\n\n详情:\n{str(e)}"
        )
        temp_root.destroy()
        sys.exit(1)