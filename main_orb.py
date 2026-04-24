# --- DPI 声明 -----------------------------------------------------
import glob
import sys
import uuid

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
import customtkinter as ctk
from tkinter import messagebox
from PIL import Image, ImageTk
import time
import config  # <--- 导入同目录下的配置文件
import os


# Windows API 常量
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

# UI主题设置
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

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
    "眠枭之星":(802,803),
    "魔力果实":(807,809)
}
MINIMAP_DATA = {}
group_text = "交流群：984599317"

# 自定义部分路径常量
CUSTOM_POINTS_DIR = "assest/custom/points"
CUSTOM_ROUTES_DIR = "assest/custom/routes"
ICON_DIR = "assest/icons"

os.makedirs(CUSTOM_POINTS_DIR, exist_ok=True)
os.makedirs(CUSTOM_ROUTES_DIR, exist_ok=True)
os.makedirs(ICON_DIR, exist_ok=True)

# 性能参数
SETTING_VAR = "HIGH"
TOOL_SETTINGS = {
    "HIGH":{
        "maxIters":2000
    },
    "LOW":{
        "maxIters":800
    }
}

def super_enhance(image, isPlayer=False): #return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # 转为灰度
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    alpha = 1.2  # 对比度系数
    beta = -40  # 亮度偏移
    enhanced = cv2.convertScaleAbs(gray, alpha=alpha, beta=beta)
    return enhanced

def generate_marker_id():
    return uuid.uuid4().hex[:20]

class BigMapWindow(ctk.CTkToplevel):
    def __init__(
            self,
            master,
            map_img,
            system_markers,
            custom_markers,
            icon_cache,
            resource_type_selected_items,
            parent_app=None
    ):
        super().__init__(master)
        self.title("全图预览 - 鼠标滚轮缩放 / 左键拖拽")
        self.geometry("1100x800")

        self.parent_app = parent_app
        self.original_img = map_img.convert("RGBA")
        self.markers = system_markers
        self.custom_markers = custom_markers
        self.icon_cache = icon_cache
        self.resource_type_selected_items = resource_type_selected_items

        # --- UI 追踪字典
        self.custom_canvas_icons = {}

        # --- 核心：烘焙两套大图（一套带系统点，一套纯净版） ---
        self.pure_map_img = self.original_img.copy()  # 纯净版地图
        self.baked_system_img = self.bake_static_map()  # 包含系统标点的地图
        self.orig_w, self.orig_h = self.baked_system_img.size

        # 预生成两套缩略图提升性能
        thumb_ratio = min(2048 / self.orig_w, 2048 / self.orig_h)
        if thumb_ratio < 1.0:
            self.system_thumbnail_img = self.baked_system_img.resize(
                (int(self.orig_w * thumb_ratio), int(self.orig_h * thumb_ratio)), Image.Resampling.BILINEAR)
            self.pure_thumbnail_img = self.pure_map_img.resize(
                (int(self.orig_w * thumb_ratio), int(self.orig_h * thumb_ratio)), Image.Resampling.BILINEAR)
            self.thumb_scale_factor = thumb_ratio
        else:
            self.system_thumbnail_img = self.baked_system_img
            self.pure_thumbnail_img = self.pure_map_img
            self.thumb_scale_factor = 1.0

        # 初始状态设为显示系统标点
        self.baked_full_image = self.baked_system_img
        self.thumbnail_img = self.system_thumbnail_img
        self.is_dragging = False
        self.scale = 0.2

        if self.parent_app and self.parent_app.smooth_x is not None:
            self.offset_x = (self.winfo_width() / 2) - self.parent_app.smooth_x * self.scale
            self.offset_y = (self.winfo_height() / 2) - self.parent_app.smooth_y * self.scale
        else:
            self.offset_x = (1000 - self.orig_w * self.scale) / 2
            self.offset_y = (800 - self.orig_h * self.scale) / 2

        # === 顶部 UI 区域
        self.top_frame = ctk.CTkFrame(self, height=40, corner_radius=0, fg_color="#2b2b2b")
        self.top_frame.pack(side=tk.TOP, fill=tk.X)

        self.btn_save_points = ctk.CTkButton(self.top_frame, text="保存标点", width=80, command=self.save_custom_points)
        self.btn_save_points.pack(side=tk.LEFT, padx=5, pady=5)

        self.btn_save_route = ctk.CTkButton(self.top_frame, text="保存连线", width=80, command=self.save_custom_route)
        self.btn_save_route.pack(side=tk.LEFT, padx=5, pady=5)

        # 仅看自定义标点勾选框
        self.show_only_custom_var = ctk.BooleanVar(value=False)
        self.chk_only_custom = ctk.CTkCheckBox(self.top_frame, text="仅看自定义", variable=self.show_only_custom_var,
                                               command=self.on_show_custom_toggle)
        self.chk_only_custom.pack(side=tk.LEFT, padx=15, pady=5)

        # 查看路线下拉框
        route_files = glob.glob(os.path.join(CUSTOM_ROUTES_DIR, "*.json"))
        route_names = ["不显示路线"] + [os.path.basename(r) for r in route_files]
        self.view_route_var = tk.StringVar(value="不显示路线")
        self.cmb_view_route = ctk.CTkComboBox(self.top_frame, values=route_names, variable=self.view_route_var,
                                              command=self.on_view_route_change)
        self.cmb_view_route.pack(side=tk.LEFT, padx=5, pady=5)

        self.canvas = tk.Canvas(self, bg='#1a1a1a', cursor="fleur")
        self.canvas.pack(fill=ctk.BOTH, expand=True)

        self.is_route_mode = False
        self.current_route_nodes = []
        self.temp_route_lines = []

        self.canvas.bind("<MouseWheel>", self.on_zoom)
        self.canvas.bind("<ButtonPress-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_drag_release)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.bind("<r>", self.toggle_route_mode)
        self.bind("<R>", self.toggle_route_mode)
        self.canvas.bind("<Button-1>", self.on_left_click, add="+")
        self.bind("<Configure>", lambda e: self.render())

        self.after(300, self.render)

    def on_show_custom_toggle(self):
        """切换底图版本：是否隐藏系统标点"""
        if self.show_only_custom_var.get():
            self.baked_full_image = self.pure_map_img
            self.thumbnail_img = self.pure_thumbnail_img
        else:
            self.baked_full_image = self.baked_system_img
            self.thumbnail_img = self.system_thumbnail_img
        self.render()

    def on_view_route_change(self, choice):
        """选择加载对应的路线文件进行预览"""
        if choice == "不显示路线":
            self.viewing_route_data = None
        else:
            filepath = os.path.join(CUSTOM_ROUTES_DIR, choice)
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    self.viewing_route_data = json.load(f)
        self.update_dynamic_ui()

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
        self.canvas.tag_lower("map_img")  # 核心：底图必须最底下
        self.update_dynamic_ui()  # 核心：重新计算所有动态标点

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

    def get_canvas_coords(self, phys_x, phys_y):
        """将原图的绝对物理坐标转换为当前 UI 画布的相对坐标"""
        canvas_x = phys_x * self.scale + self.offset_x
        canvas_y = phys_y * self.scale + self.offset_y
        return canvas_x, canvas_y

    def get_physical_coords(self, canvas_x, canvas_y):
        """将用户点击的画布坐标还原为游戏物理坐标"""
        phys_x = (canvas_x - self.offset_x) / self.scale
        phys_y = (canvas_y - self.offset_y) / self.scale
        return phys_x, phys_y

    def get_marker_by_id(self, marker_id):
        """通过 ID 查找标点（同时遍历基础点位和新增自定义点位）"""
        for m in self.markers + self.custom_markers:
            if m['id'] == marker_id:
                return m
        return None

    def clear_route_highlight(self):
        """清理连线模式的临时高亮圈和黄线"""
        for node in self.current_route_nodes:
            if 'highlight_id' in node:
                self.canvas.delete(node['highlight_id'])
        for line_id in self.temp_route_lines:
            self.canvas.delete(line_id)

        self.current_route_nodes = []
        self.temp_route_lines = []

    def render_single_marker(self, marker):
        """在画布上动态渲染单个自定义标点（无需重新烘焙整张地图）"""
        m_type = str(marker['markType'])
        icon_set = self.icon_cache.get(m_type)
        if icon_set:
            cx, cy = self.get_canvas_coords(marker['pixel_x'], marker['pixel_y'])
            # 创建 Canvas 图像，并记录 ID 用于后续移动或删除
            marker['canvas_id'] = self.canvas.create_image(
                cx, cy, anchor=tk.CENTER, image=icon_set["tk_normal"], tags="custom_marker"
            )
            self.canvas.tag_raise(marker['canvas_id'])

    def update_marker_icon(self, marker, new_icon_name, window):
        """更新单个标点的图标 (覆盖你原本留空的这个方法)"""
        marker['markType'] = new_icon_name
        icon_set = self.icon_cache.get(str(new_icon_name))
        if icon_set and 'canvas_id' in marker:
            self.canvas.itemconfig(marker['canvas_id'], image=icon_set["tk_normal"])
        window.destroy()

    def update_dynamic_ui(self):
        """所有非烘焙元素的实时对齐与重绘 (彻底告别幽灵标点)"""
        if not hasattr(self, 'custom_canvas_icons'):
            self.custom_canvas_icons = {}

        # 自动对齐自定义标点
        for m in self.custom_markers:
            cx, cy = self.get_canvas_coords(m['pixel_x'], m['pixel_y'])
            icon_set = self.icon_cache.get(str(m.get('type', '301')))
            m_id = m['id']

            if m_id not in self.custom_canvas_icons:
                if icon_set:
                    item_id = self.canvas.create_image(cx, cy, anchor=tk.CENTER, image=icon_set["tk_normal"],
                                                       tags="custom_marker")
                    self.custom_canvas_icons[m_id] = item_id
            else:
                item_id = self.custom_canvas_icons[m_id]
                self.canvas.coords(item_id, cx, cy)
                if icon_set:
                    self.canvas.itemconfig(item_id, image=icon_set["tk_normal"])
                self.canvas.tag_raise(item_id)

        # 路线节点与虚线逻辑
        for node in self.current_route_nodes:
            if 'highlight_id' in node:
                m = self.get_marker_by_id(node['id'])
                if m:
                    cx, cy = self.get_canvas_coords(m['pixel_x'], m['pixel_y'])
                    self.canvas.coords(node['highlight_id'], cx - 10, cy - 10, cx + 10, cy + 10)
                    self.canvas.tag_raise(node['highlight_id'])

        if len(self.current_route_nodes) > 1 and len(self.temp_route_lines) == len(self.current_route_nodes) - 1:
            for i in range(len(self.temp_route_lines)):
                m1 = self.get_marker_by_id(self.current_route_nodes[i]['id'])
                m2 = self.get_marker_by_id(self.current_route_nodes[i + 1]['id'])
                if m1 and m2:
                    x1, y1 = self.get_canvas_coords(m1['pixel_x'], m1['pixel_y'])
                    x2, y2 = self.get_canvas_coords(m2['pixel_x'], m2['pixel_y'])
                    self.canvas.coords(self.temp_route_lines[i], x1, y1, x2, y2)
                    self.canvas.tag_raise(self.temp_route_lines[i])

        # 绘制预览的路线
        self.canvas.delete("view_route_line")
        if getattr(self, 'viewing_route_data', None):
            valid_nodes = []
            for node in self.viewing_route_data.get('nodes', []):
                m = self.get_marker_by_id(node['id'])
                if m: valid_nodes.append(m)

            for i in range(len(valid_nodes) - 1):
                m1, m2 = valid_nodes[i], valid_nodes[i + 1]
                x1, y1 = self.get_canvas_coords(m1['pixel_x'], m1['pixel_y'])
                x2, y2 = self.get_canvas_coords(m2['pixel_x'], m2['pixel_y'])
                self.canvas.create_line(x1, y1, x2, y2, fill="#FF00FF", width=3, dash=(4, 2), arrow=tk.LAST,
                                        tags="view_route_line")

        self.canvas.tag_raise("custom_marker")

    def on_right_click(self, event):
        if self.is_route_mode: return

        clicked_marker = None
        is_custom = False

        # 开启了"仅看自定义"，就不遍历系统标点
        all_markers = self.custom_markers if self.show_only_custom_var.get() else self.markers + self.custom_markers

        for m in all_markers:
            cx, cy = self.get_canvas_coords(m['pixel_x'], m['pixel_y'])
            if (cx - event.x) ** 2 + (cy - event.y) ** 2 < 225:
                clicked_marker = m
                is_custom = m.get('is_custom', False)
                break

        # 创建右键菜单
        menu = tk.Menu(self, tearoff=0)

        if clicked_marker:
            if is_custom:
                menu.add_command(label="编辑标点", command=lambda: self.edit_marker(clicked_marker))
                menu.add_command(label="删除标点", command=lambda: self.delete_marker(clicked_marker))
            else:
                # 点击了系统自带标点，仅提示
                menu.add_command(label="系统标点无法修改", state="disabled")
        else:
            # 点击了空地
            menu.add_command(label="创建标点", command=lambda: self.create_marker(event.x, event.y))

        menu.tk_popup(event.x_root, event.y_root)

    def create_marker(self, canvas_x, canvas_y):
        phys_x, phys_y = self.get_physical_coords(canvas_x, canvas_y)
        new_marker = {
            "id": generate_marker_id(),
            "type": "301",
            "markType": "301",
            "pixel_x": phys_x,
            "pixel_y": phys_y,
            "is_custom": True,
            "is_collected": False
        }
        # 将新点位通知给主程序，主程序和此窗口共享该数据！
        if self.parent_app:
            self.parent_app.on_custom_marker_added(new_marker)
        else:
            self.custom_markers.append(new_marker)

        self.update_dynamic_ui()  # 立刻刷新

    def delete_marker(self, marker):
        if self.parent_app:
            self.parent_app.on_custom_marker_deleted(marker)
        else:
            self.custom_markers.remove(marker)

        m_id = marker['id']
        if hasattr(self, 'custom_canvas_icons') and m_id in self.custom_canvas_icons:
            self.canvas.delete(self.custom_canvas_icons[m_id])
            del self.custom_canvas_icons[m_id]

    def edit_marker(self, marker):
        """弹出带滚动条的可视化图标选择窗口"""
        top = ctk.CTkToplevel(self)
        top.title("选择图标")
        top.geometry("500x500")
        top.attributes("-topmost", True)  # 确保窗口不被大地图遮挡

        # 创建自带滚动条的框架
        scroll_frame = ctk.CTkScrollableFrame(top)
        scroll_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        row, col = 0, 0

        # 遍历程序启动时已经加载好的 icon_cache
        for icon_name, icon_data in self.icon_cache.items():
            # 使用标准 tk.Button 因为它与 ImageTk.PhotoImage 兼容性完美
            # 背景色设为深色以契合夜间模式
            btn = tk.Button(
                scroll_frame,
                image=icon_data["tk_normal"],
                relief=tk.FLAT,
                bg="#3b3b3b",
                activebackground="#5a5a5a",
                cursor="hand2",
                command=lambda n=icon_name: self.update_marker_icon(marker, n, top)
            )
            btn.grid(row=row, column=col, padx=8, pady=8)

            col += 1
            if col > 7:  # 每排显示 8 个图标
                col = 0
                row += 1

    def update_marker_icon(self, marker, new_icon_name, window):
        marker['type'] = str(new_icon_name)      # 同步主程序标识
        marker['markType'] = str(new_icon_name)  # 同步本地标识
        self.update_dynamic_ui()                 # 立刻刷新图标
        window.destroy()

    def save_custom_points(self):
        """修复保存逻辑：遍历整个共享池"""
        if not self.custom_markers:
            messagebox.showinfo("提示", "当前没有任何自定义标点")
            return

        export_data = {}
        for m in self.custom_markers:
            m_type = str(m.get('type', m.get('markType', '301')))
            if m_type not in export_data:
                export_data[m_type] = []

            export_data[m_type].append({
                "markType": m_type,
                "id": m['id'],
                "point": {
                    "lat": m['pixel_y'],
                    "lng": m['pixel_x']
                }
            })

        filepath = os.path.join(CUSTOM_POINTS_DIR, "user_points.json")
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("成功", f"标点已保存至 {filepath}")

    def toggle_route_mode(self, event=None):
        self.is_route_mode = not self.is_route_mode
        if self.is_route_mode:
            messagebox.showinfo("模式切换", "目前是路径定义模式，请左键点击标点进行连线。")
        else:
            # 退出模式，清理临时高亮状态，但不清理数据
            self.clear_route_highlight()
            messagebox.showinfo("模式切换", "已退出路径定义模式。")

    def on_left_click(self, event):
        if not self.is_route_mode:
            return  # 仅在路径模式下拦截左键

        # 查找被点击的标点
        all_markers = self.markers + self.custom_markers
        clicked_marker = None
        for m in all_markers:
            cx, cy = self.get_canvas_coords(m['pixel_x'], m['pixel_y'])
            if (cx - event.x) ** 2 + (cy - event.y) ** 2 < 225:
                clicked_marker = m
                break

        if not clicked_marker:
            return

        # 检查是否已经被连过线
        if any(node['id'] == clicked_marker['id'] for node in self.current_route_nodes):
            return  # 已存在则忽略

        # 1. 变绿高亮 (假设图标有外框，或者直接在图标下面画一个绿圈)
        cx, cy = self.get_canvas_coords(clicked_marker['pixel_x'], clicked_marker['pixel_y'])
        hl_id = self.canvas.create_oval(cx - 10, cy - 10, cx + 10, cy + 10, outline="green", width=3)

        # 2. 如果不是第一个点，连上黄色的线
        if len(self.current_route_nodes) > 0:
            prev_node = self.current_route_nodes[-1]
            prev_m = self.get_marker_by_id(prev_node['id'])
            px, py = self.get_canvas_coords(prev_m['pixel_x'], prev_m['pixel_y'])

            line_id = self.canvas.create_line(px, py, cx, cy, fill="yellow", width=2, dash=(4, 2))
            self.temp_route_lines.append(line_id)

        # 3. 加入路线列表
        self.current_route_nodes.append({
            "id": clicked_marker['id'],
            "is_custom": clicked_marker.get('is_custom', False),
            "highlight_id": hl_id
        })

    def save_custom_route(self):
        if len(self.current_route_nodes) < 2:
            messagebox.showwarning("警告", "路线至少需要包含2个标点！")
            return

        # 要求极强的拓展性和易维护性，采用带有元数据的 JSON 结构
        route_data = {
            "version": "1.0",
            "route_name": f"route_{generate_marker_id()[:6]}",
            "node_count": len(self.current_route_nodes),
            "nodes": [
                {
                    "order": idx,
                    "id": node['id'],
                    "is_custom": node['is_custom']
                } for idx, node in enumerate(self.current_route_nodes)
            ]
        }

        filepath = os.path.join(CUSTOM_ROUTES_DIR, f"{route_data['route_name']}.json")
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(route_data, f, ensure_ascii=False, indent=2)

        messagebox.showinfo("成功", f"路线已保存至 {filepath}")
        self.clear_route_highlight()  # 保存后清理
        self.current_route_nodes = []
        if self.parent_app:
            self.parent_app.refresh_route_list()



class MapTrackerApp:
    def __init__(self, root):
        log_step("正在尝试建立主root")
        self.root = root

        self.root.title(f"{group_text}")
        self.status_text_id = None

        # --- 窗口属性设置 ---
        self.root.attributes("-topmost", True)
        # --- 使用配置文件中的悬浮窗几何设置 ---
        self.root.geometry(WINDOW_GEOMETRY)
        # --- 使用配置文件中的截图区域
        self.minimap_region = MINIMAP_DATA #config.MINIMAP
        self.last_pos = None

        # --- UI初始化 ---
        log_step("正在初始化UI")
        self.status_label = ctk.CTkLabel(
            root,
            text="软件加载，请稍候...",
            font=("微软雅黑", 12))
        self.status_label.pack(side=ctk.TOP, fill=ctk.X, pady=5)
        self.root.after(100, self.ui_delayed_init)

        # UI 组件
        # --- 使用配置文件中的悬浮窗视野大小 (VIEW_SIZE)
        log_step("尝试加载UI组件")
        self.canvas_container = ctk.CTkFrame(root, fg_color="transparent")
        self.canvas_container.pack(fill="both", expand=True, padx=5, pady=5)

        self.canvas = tk.Canvas(
            self.canvas_container,
            bg='#EBEBEB',
            highlightthickness=0,
            borderwidth=0,
            cursor="cross"
        )
        #self.canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.canvas.pack(fill="both", expand=True)


        self.image_on_canvas = None

        print(MINIMAP_DATA)

        # UI底部控制区
        self.ctrl_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        self.ctrl_frame.pack(
            side=ctk.BOTTOM,
            fill=ctk.X,
            padx=5,
            pady=5
        )
        # 隐藏 UI 面板的状态和按钮
        self.ui_hidden = False

        # 这个小按钮放在最外层，用于面板被隐藏后让用户可以重新点出来
        self.btn_show_ctrl = ctk.CTkButton(self.root, text="▼ 展开控制面板 (Alt+I)", height=24, command=self.toggle_ctrl_frame)

        # 在控制面板内部放一个隐藏按钮
        self.btn_hide_ctrl = ctk.CTkButton(self.ctrl_frame, text="▲ 隐藏面板 (Alt+I)", fg_color="#555555", hover_color="#333333", command=self.toggle_ctrl_frame)
        self.btn_hide_ctrl.pack(side=ctk.BOTTOM, fill=ctk.X, pady=(10, 0))

        # UI锁定-用于全屏
        # -- 开启UI锁定
        self.ui_lock_var = ctk.BooleanVar(value=False)
        self.ui_lock_cb = ctk.CTkCheckBox(
            self.ctrl_frame,
            text="开启UI锁定 (Alt+L) *需要管理员模式运行",
            variable=self.ui_lock_var,
            command = self.toggle_ui_lock_from_cb  # 绑定点击事件
        )
        self.ui_lock_cb.pack(side=ctk.BOTTOM, fill=ctk.X,pady=5)
        self.start_hotkey_listener() #启用快捷键监听

        # 玩家箭头显示开启
        self.player_arrow_enable_var = ctk.BooleanVar(value=True)
        self.player_arrow_enable_cb = ctk.CTkCheckBox(
            self.ctrl_frame, text="开启玩家箭头显示",
            variable=self.player_arrow_enable_var,
        )
        self.player_arrow_enable_cb.pack(side=ctk.BOTTOM, fill=tk.X, pady=5)

        # 重置按钮
        log_step("正在加载按钮")
        self.reset_btn = ctk.CTkButton(
            self.ctrl_frame, text="手动重置定位 (全图扫描)",
            command=self.reset_location,
        )
        self.reset_btn.pack(side=ctk.BOTTOM, fill=tk.X,pady=5)  # 放在底部并水平铺满

        # -- 清空已采集按钮
        self.reset_collect_btn = ctk.CTkButton(
            self.ctrl_frame, text="重置所有已采集",
            fg_color="#4a2b2b", hover_color="#6e3a3a",
            command=self.reset_picking_data,
        )
        # 放在自动采集开关下方，全图预览上方
        self.reset_collect_btn.pack(side=ctk.BOTTOM, fill=tk.X,pady=5)


        # -- 下拉选择资源类型
        log_step("正在加载下拉菜单")
        self.resource_type_options = []
        self.resource_type_selected_items = []
        self.resource_type_vars = {}
        self.resource_type_popup = None
        self.resource_type_text = "请选择"
        self.resource_type_button = ctk.CTkButton(
            self.ctrl_frame, text=self.resource_type_text,
            command=self.resource_type_toggle_popup)
        self.resource_type_button.pack(side=ctk.BOTTOM,fill="x", expand=True,pady=5)


        # -- 大地图按钮
        log_step("正在加载大地图按钮")
        self.big_map_btn = ctk.CTkButton(
            self.ctrl_frame, text="打开大地图预览", command=self.open_big_map
        )
        self.big_map_btn.pack(side=ctk.BOTTOM, fill=ctk.X,pady=5)

        # 地图缩放控制
        self.zoom_var = ctk.DoubleVar(value=1.0)
        self.zoom_frame = ctk.CTkFrame(self.ctrl_frame, fg_color="transparent")
        self.zoom_frame.pack(side=ctk.BOTTOM, fill=tk.X, pady=5)

        self.zoom_label = ctk.CTkLabel(self.zoom_frame, text="地图缩放: 1.0x", width=80)
        self.zoom_label.pack(side=ctk.LEFT, padx=(0, 5))

        def on_zoom_change(val):
            self.zoom_label.configure(text=f"地图缩放: {float(val):.1f}x")

        self.zoom_slider = ctk.CTkSlider(
            self.zoom_frame,
            from_=0.5, to=2.0,
            variable=self.zoom_var,
            number_of_steps=12,
            command=on_zoom_change
        )
        self.zoom_slider.pack(side=ctk.LEFT, fill=tk.X, expand=True)

        # -- 开启图标自动标记
        self.auto_collect_var = ctk.BooleanVar(value=False)
        self.auto_collect_cb = ctk.CTkCheckBox(
            self.ctrl_frame, text="开启图标自动标记",
            variable=self.auto_collect_var,
        )
        self.auto_collect_cb.pack(side=ctk.BOTTOM, fill=tk.X, pady=5)

        # -- 开启最近路线规划
        self.auto_route_planning_var = ctk.BooleanVar(value=False)
        self.auto_route_planning_cb = ctk.CTkCheckBox(
            self.ctrl_frame, text="开启最近路线规划(与自定义路线冲突)",
            variable=self.auto_route_planning_var,
            command=self.on_auto_route_toggle
        )
        self.auto_route_planning_cb.pack(side=ctk.BOTTOM, fill=tk.X, pady=5)

        # -- 自定义路线
        self.available_routes = glob.glob(os.path.join(CUSTOM_ROUTES_DIR, "*.json"))
        route_names = [os.path.basename(r) for r in self.available_routes]
        self.route_var = tk.StringVar(value=route_names[0] if route_names else "")
        self.use_custom_route_var = tk.BooleanVar(value=False)

        # -- 下拉选择框
        self.chk_custom_route = ctk.CTkCheckBox(
            self.ctrl_frame, text="启用自定义路线",
            variable=self.use_custom_route_var,
            command=self.on_route_toggle)
        self.chk_custom_route.pack(side=ctk.BOTTOM, fill=tk.X, pady=5)

        self.route_combobox = ctk.CTkComboBox(
            self.ctrl_frame,
            values=route_names,
            variable=self.route_var,
            command=self.on_route_combobox_change  # 增加事件联动
        )
        self.route_combobox.pack(side=ctk.BOTTOM, fill=tk.X,pady=5)

        # 多线程初始化
        log_step("尝试初始化多线程")
        self.frame_queue = queue.Queue(maxsize=1) # 队列初始化
        self.is_running = True

        self.current_pos = (None, None)  # 存储计算线程算出的最新坐标

        import collections
        self.pos_history_x = collections.deque(maxlen=5)  # 保留最近5帧
        self.pos_history_y = collections.deque(maxlen=5)



        # 启动截图线程
        self.minimap_np = None
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

        # --- 用于方向识别的变量
        self.current_angle = 0.0  # 目标角度

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
        self.resource_type_button.configure(text=f"过滤资源: 已选 {len(self.resource_type_options)} 类")

        self.marker_data = self.load_markers(config.POINTS_PATH)
        self.custom_markers = self.load_custom_markers()
        self.marker_data.extend(self.custom_markers)  # 将自定义标点合并到总池，享受同等渲染和自动采集待遇

        self.markers_np_coords = np.array([[m['pixel_x'], m['pixel_y']] for m in self.marker_data], dtype=np.float32)
        # 加载图标并缓存（包含灰色版本）
        self.icon_cache = self.prep_icons(r"assest/icons")
        log_step(f"已加载图标并缓存")

        # 屏幕截图设置 (MSS)
        self.sct = mss.mss()
        # --- 预先生成小地图的掩模 (Mask)
        mask_h = MINIMAP_DATA.get("height", 256)  # 替换为你的真实高度
        mask_w = MINIMAP_DATA.get("width", 256)  # 替换为你的真实宽度
        self.minimap_mask = np.zeros((mask_h, mask_w), dtype=np.uint8)
        cv2.circle(self.minimap_mask, (mask_w // 2, mask_h // 2), (mask_w // 2) - 5, 255, -1)
        # --- 预先定义小地图的中心点坐标 (用于后面的透视变换计算)
        self.mini_center_pt = np.float32([[[mask_w / 2, mask_h / 2]]])
        log_step(f"已获取小地图范围Mask")

        self.status_label.destroy()

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

                    # 获取当前画板真实的物理尺寸
                    view_w = self.canvas.winfo_width()
                    view_h = self.canvas.winfo_height()

                    # 防御性判定：Tkinter 刚启动时 winfo_width 可能返回 1，此时使用配置作为兜底
                    if view_w <= 10 or view_h <= 10:
                        view_w, view_h = config.VIEW_SIZE, config.VIEW_SIZE

                    # 获取缩放比例并计算裁剪尺寸
                    zoom_scale = self.zoom_var.get()

                    # 基于缩放反推需要在大地图上截取的物理范围大小
                    crop_w = max(10, int(view_w / zoom_scale))
                    crop_h = max(10, int(view_h / zoom_scale))
                    half_crop_w, half_crop_h = crop_w // 2, crop_h // 2

                    x1, y1 = int(center_x - half_crop_w), int(center_y - half_crop_h)
                    x2, y2 = x1 + crop_w, y1 + crop_h

                    # 动态生成用于裁剪的原图底板
                    bg_crop = np.zeros((crop_h, crop_w, 3), dtype=np.uint8)
                    bg_crop[:] = (43, 43, 43)

                    # 计算在原图上截取的合法范围
                    map_x1, map_y1 = max(0, x1), max(0, y1)
                    map_x2, map_y2 = min(self.map_width, x2), min(self.map_height, y2)

                    # 只有当截取范围有效时才进行像素复制
                    if map_x1 < map_x2 and map_y1 < map_y2:
                        paste_x1 = map_x1 - x1
                        paste_y1 = map_y1 - y1
                        paste_x2 = paste_x1 + (map_x2 - map_x1)
                        paste_y2 = paste_y1 + (map_y2 - map_y1)

                        # 将合法部分的地图贴到底板上，确保坐标系绝对对齐
                        bg_crop[paste_y1:paste_y2, paste_x1:paste_x2] = self.logic_map_bgr[map_y1:map_y2, map_x1:map_x2]

                    # 根据缩放比例通过 OpenCV 将原图缩放到视口大小
                    if zoom_scale != 1.0:
                        bg_canvas = cv2.resize(bg_crop, (view_w, view_h), interpolation=cv2.INTER_LINEAR)
                    else:
                        bg_canvas = bg_crop

                    # 将拼合好的底板转换为图片
                    pil_bg = Image.fromarray(cv2.cvtColor(bg_canvas, cv2.COLOR_BGR2RGB))
                    self.tk_bg_image = ImageTk.PhotoImage(pil_bg)

                    # 更新底层背景图片（如果不存在则创建）
                    if not hasattr(self, 'bg_image_id') or self.bg_image_id is None:
                        self.bg_image_id = self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_bg_image)
                        self.canvas.tag_lower(self.bg_image_id)  # 确保底图永远在最下层
                    else:
                        self.canvas.itemconfig(self.bg_image_id, image=self.tk_bg_image, state="normal")

                    # 清除渲染堆叠
                    self.canvas.delete("player_indicator")
                    self.canvas.delete("route_line") #旧路线

                    # 绘制玩家位置箭头与圆圈参数预计算
                    ui_center_x = view_w // 2
                    ui_center_y = view_h // 2
                    radius = 8  # 圆圈半径

                    bbox = [ui_center_x - radius, ui_center_y - radius,
                            ui_center_x + radius, ui_center_y + radius]

                    radius_picking = radius + int(config.PICKING_RADIUS * (view_w / crop_w)) # 黄圈在 UI 视觉上跟随底图同步放大或缩小
                    bbox_picking = [ui_center_x - radius_picking, ui_center_y - radius_picking,
                            ui_center_x + radius_picking, ui_center_y + radius_picking]

                    if (
                            self.player_arrow_enable_var.get()
                            and self.minimap_np is not None
                    ):
                        # --- 截取小地图中心图案并贴在 UI 上 ---
                        try:
                            h_mini, w_mini = self.minimap_np.shape[:2]
                            cx, cy = w_mini // 2, h_mini // 2

                            crop_radius = 30  # 设置截取半径 默认 30

                            # 动态计算玩家箭头的显示尺寸
                            base_display_size = 68  # 默认尺寸 68
                            current_zoom = view_w / crop_w if 'crop_w' in locals() and crop_w > 0 else 1.0
                            display_size = max(16, int(base_display_size * current_zoom)) # 乘以缩放倍率，并限制最小尺寸防崩溃

                            opacity = 0.7  # 透明度

                            # 防止画面边缘越界保护
                            if cy - crop_radius >= 0 and cy + crop_radius <= h_mini and cx - crop_radius >= 0 and cx + crop_radius <= w_mini:
                                # 截取
                                roi = self.minimap_np[
                                    cy - crop_radius: cy + crop_radius, cx - crop_radius: cx + crop_radius]

                                # 转换颜色和透明度
                                rgba_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2RGBA)

                                # 制作圆形遮罩 (注意：遮罩大小需与 crop_radius 对应)
                                mask_size = crop_radius * 2
                                mask = np.zeros((mask_size, mask_size), dtype=np.uint8)
                                alpha_value = int(255 * opacity)
                                cv2.circle(mask, (crop_radius, crop_radius), crop_radius, alpha_value, -1)
                                rgba_roi[:, :, 3] = mask


                                # 转换为 PIL 对象
                                pil_arrow = Image.fromarray(rgba_roi)

                                # 使用 LANCZOS 进行高质量缩放
                                pil_arrow = pil_arrow.resize((display_size, display_size), Image.Resampling.LANCZOS)

                                self.tk_player_arrow = ImageTk.PhotoImage(pil_arrow)

                                # 绘制到画板中心
                                self.canvas.create_image(
                                    ui_center_x, ui_center_y,
                                    anchor=tk.CENTER,
                                    image=self.tk_player_arrow,
                                    tags="player_indicator"
                                )
                        except Exception as e:
                            log_step(f"小地图箭头截取/缩放报错：{e}")


                    # 独立管理图标 (不修改底图像素)
                    if not hasattr(self, 'canvas_icons'):
                        self.canvas_icons = {}

                    # 快速过滤出在矩形范围内的点的索引
                    valid_idx = np.where(
                        (self.markers_np_coords[:, 0] >= x1) &
                        (self.markers_np_coords[:, 0] <= x2) &
                        (self.markers_np_coords[:, 1] >= y1) &
                        (self.markers_np_coords[:, 1] <= y2)
                    )[0]

                    # 隐藏不在范围内的点
                    visible_ids = set([self.marker_data[i]['id'] for i in valid_idx])
                    for m_id, item_id in self.canvas_icons.items():
                        if m_id not in visible_ids:
                            self.canvas.itemconfig(item_id, state="hidden")

                    # 仅对视锥范围内的点进行精准计算和渲染
                    cull_dist = max(250, int(250 / zoom_scale))
                    for i in valid_idx:
                        m = self.marker_data[i]
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
                            dist = ((m['pixel_x'] - center_x) ** 2 + (m['pixel_y'] - center_y) ** 2) ** 0.5

                            # ---------- [修改开始] 根据缩放比例动态调整剔除距离阈值 ----------
                            cull_dist = max(250, int(250 / zoom_scale))
                            if dist > cull_dist:
                                # 距离过远，如果在画布上则隐藏
                                if m_id in self.canvas_icons:
                                    self.canvas.itemconfig(self.canvas_icons[m_id], state="hidden")
                                continue

                            # 自动采集逻辑 (距离判定保持原大地图物理距离，不受缩放干扰)
                            if self.auto_collect_var.get() and dist < config.PICKING_RADIUS and not m['is_collected']:
                                m['is_collected'] = True
                                need_save = True
                                if DEBUG_MODE:
                                    log_step(f"DEBUG: 自动采集资源点 {m['id']} (类型: {m['type']})")

                            # 计算在 Canvas 上的相对坐标，并加入真实的缩放乘数因子
                            rx = int((m['pixel_x'] - x1) * (view_w / crop_w))
                            ry = int((m['pixel_y'] - y1) * (view_h / crop_h))

                            icon_set = self.icon_cache.get(m['type'])
                            if not icon_set: continue

                            target_img = icon_set["tk_gray"] if m['is_collected'] else icon_set["tk_normal"]

                            # 如果 Canvas 上还没这个图标，创建它
                            if m_id not in self.canvas_icons:
                                item_id = self.canvas.create_image(
                                    rx, ry,
                                    anchor=tk.CENTER,
                                    image=target_img,
                                    tags="resource_icon"
                                )
                                self.canvas_icons[m_id] = item_id
                            else:
                                # 如果已存在，仅更新位置、图片和状态（恢复显示）
                                item_id = self.canvas_icons[m_id]
                                self.canvas.itemconfig(item_id, image=target_img, state="normal", tags="resource_icon")
                                self.canvas.coords(item_id, rx, ry)
                        else:
                            # 视野外，如果有对应的 item 则隐藏
                            if m_id in self.canvas_icons:
                                self.canvas.itemconfig(self.canvas_icons[m_id], state="hidden")

                    # 路线规划绘制逻辑
                    if (
                            getattr(self, 'auto_route_planning_var',None)
                            and self.auto_route_planning_var.get()
                            and self.found
                    ):
                        route_markers = self.calculate_collection_route(self.smooth_x, self.smooth_y, num_points=10)
                        if route_markers:
                            # ---------- [修改开始] ----------
                            # 路线的起点是屏幕中心的玩家位置
                            # (原代码这里有 Bug: center_x 是大地图坐标，会导致连线从几千像素外飞来，修复为 UI 画布中心点坐标)
                            prev_x, prev_y = ui_center_x, ui_center_y

                            for idx, m in enumerate(route_markers):
                                # 将大地图绝对坐标转换为 Canvas 相对坐标，并适配缩放
                                rx = int((m['pixel_x'] - x1) * (view_w / crop_w))
                                ry = int((m['pixel_y'] - y1) * (view_h / crop_h))

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

                    if getattr(self, 'use_custom_route_var', None) and self.use_custom_route_var.get() and self.found:
                        self.render_active_route(x1, y1, view_w, crop_w, view_h, crop_h, ui_center_x, ui_center_y)

                    # 采集范围圈
                    self.canvas.create_oval(bbox_picking, outline="yellow",dash=(4,2), width=1, tags="player_indicator")
                    if self.found:
                        # 定位成功：画红圈
                        if not self.player_arrow_enable_var.get():
                            self.canvas.create_oval(bbox, outline="red", width=2, tags="player_indicator")
                    else:
                        # 定位丢失：画白圈
                        self.canvas.create_oval(bbox, outline="white", width=2, tags="player_indicator")

                    # 强制将所有资源图标和路线提升到最高层级
                    self.canvas.tag_raise("resource_icon")
                    self.canvas.tag_raise("route_line")
            else:
                # 隐藏地图底图和所有图标
                if hasattr(self, 'bg_image_id') and self.bg_image_id:
                    self.canvas.itemconfig(self.bg_image_id, state="hidden")

                # 隐藏所有动态图标 (利用 tags 批量操作)
                self.canvas.itemconfigure("all", state="hidden")

                # 显示或更新提示文字
                self.empty_display_text = ("计算匹配定位锚点中...\n"
                                           "请勿用任何窗口遮挡小地图，包括本软件！\n"
                                           "建议全屏游戏以保证小地图显示最大\n\n"
                                           "在此页面卡住超过10分钟\n"
                                           "是win显示设置缩放不为100%导致的")

                # 再次获取真实尺寸（针对初始还没定位成功时的画布拉伸）
                view_w = self.canvas.winfo_width()
                view_h = self.canvas.winfo_height()
                if view_w <= 10 or view_h <= 10:
                    view_w, view_h = config.VIEW_SIZE, config.VIEW_SIZE

                if not self.status_text_id:
                    self.status_text_id = self.canvas.create_text(
                        view_w // 2, view_h // 2,
                        text=self.empty_display_text,
                        fill="black",
                        font=("微软雅黑", 14, "bold"),
                        anchor="center",
                        justify="center",
                        tags="status_msg"
                    )
                else:
                    self.canvas.itemconfig(self.status_text_id, state="normal", text=self.empty_display_text)
                    # 动态更新坐标，确保窗口被拖拽时文字始终居中
                    self.canvas.coords(self.status_text_id, view_w // 2, view_h // 2)
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
        # 获取当前需要显示的、未采集的可用资源点
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

        # 提取坐标为 NumPy 数组
        coords = np.array([[m['pixel_x'], m['pixel_y']] for m in valid_markers])
        candidates = valid_markers.copy()
        route = []

        curr_pt = np.array([start_x, start_y])

        for _ in range(min(num_points, len(candidates))):
            # 向量化计算当前点到所有候选点的距离平方
            dists_sq = np.sum((coords - curr_pt) ** 2, axis=1)
            nearest_idx = np.argmin(dists_sq)

            nearest_m = candidates[nearest_idx]
            route.append(nearest_m)

            # 更新当前点
            curr_pt = coords[nearest_idx]

            # 从候选池中删除已选点
            candidates.pop(nearest_idx)
            coords = np.delete(coords, nearest_idx, axis=0)

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
                if MINIMAP_DATA == {}:
                    time.sleep(0.1)
                    continue
                try:
                    # 截图
                    screenshot = sct.grab(MINIMAP_DATA)
                    minimap_bgr = np.array(screenshot)
                    self.minimap_np = minimap_bgr[:, :, :3]

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

                if des_mini is not None and len(kp_mini) >= MIN_MATCH_COUNT:
                    is_global_mode = (self.last_x is None) or (
                                self.consecutive_failures >= self.global_search_threshold)

                    # --- 坐标系划分 ---
                    if is_global_mode:
                        if self.last_x is not None:
                            log_step("定位丢失：重置参考坐标并开启全图扫描...")
                            self.last_x = None
                        current_des_big = self.des_big
                        current_kp_big = self.kp_big
                    else:
                        # 局部搜索
                        dist_sq = (self.pts_big_np[:, 0] - self.last_x) ** 2 + (
                                    self.pts_big_np[:, 1] - self.last_y) ** 2
                        search_radius = 800 + (self.consecutive_failures * 200)
                        near_indices = np.where(dist_sq < search_radius ** 2)[0]

                        if len(near_indices) > 20:
                            current_des_big = self.des_big[near_indices]
                            # current_kp_big = [self.kp_big[i] for i in near_indices]
                        else:
                            self.consecutive_failures = self.global_search_threshold
                            continue

                    if MATCHTYPE == "BF":
                        # k=2 表示返回最近的两个匹配点
                        matches = self.bf.knnMatch(des_mini, current_des_big, k=2)
                    elif MATCHTYPE == "FLANN":
                        matches = self.flann.knnMatch(des_mini, current_des_big, k=2)

                    good_matches = []
                    # 使用配置中的比例，或者写死 0.75
                    ratio_thresh = getattr(config, 'ORB_RATIO', config.ORB_RATIO)

                    for match_pair in matches:
                        if len(match_pair) == 2:
                            m, n = match_pair
                            # 核心比率测试：最优匹配的距离必须明显小于次优匹配
                            if m.distance < ratio_thresh * n.distance:
                                good_matches.append(m)
                        elif len(match_pair) == 1:
                            good_matches.append(match_pair[0])

                    # 取质量最高的前 100 个点即可，避免过多反而引入噪点
                    good_matches = sorted(good_matches, key=lambda x: x.distance)[:100]

                    if len(good_matches) >= config.ORB_MIN_MATCH_COUNT:
                        src_pts = np.float32([kp_mini[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)

                        if is_global_mode:
                            dst_pts = np.float32([current_kp_big[m.trainIdx].pt for m in good_matches]).reshape(-1, 1,2)
                        else:
                            dst_pts = np.float32([self.kp_big[near_indices[m.trainIdx]].pt for m in good_matches]).reshape(-1, 1, 2)

                        # 降低重投影误差阈值到 1.5，提高精度；增加迭代次数，保证能找到最优解
                        M, inliers = cv2.estimateAffinePartial2D(
                            src_pts, dst_pts,
                            method=cv2.RANSAC,
                            ransacReprojThreshold=1.5,
                            maxIters=TOOL_SETTINGS.get(str(SETTING_VAR)).get("maxIters"),
                            confidence=0.98
                        )

                        if M is not None:
                            s = np.sqrt(M[0, 0] ** 2 + M[0, 1] ** 2) #部分仿射变换

                            # 游戏地图通常是 1:1，缩放应该极其接近 1.0
                            if 0.6 <= s <= 1.4:
                                # dst = cv2.perspectiveTransform(self.mini_center_pt, M) # 单应性矩阵
                                center_h = np.array([[[MINIMAP_DATA.get("width") / 2, MINIMAP_DATA.get("height")  / 2]]], dtype=np.float32)
                                dst = cv2.transform(center_h, M)
                                raw_x, raw_y = int(dst[0][0][0]), int(dst[0][0][1])

                                # 距离校验
                                if self.last_x is not None:
                                    if not is_global_mode:
                                        dist = np.sqrt((raw_x - self.last_x) ** 2 + (raw_y - self.last_y) ** 2)
                                        # 假设玩家 0.1 秒内不可能跑过 100 像素
                                        if dist > 200:
                                            self.consecutive_failures += 1
                                            log_step("-》匹配结果跳变异常，放弃更新")
                                            continue

                                # 中位数滤波
                                if (0 <= raw_x <= self.map_width) and (0 <= raw_y <= self.map_height):
                                    # 将当前计算坐标加入队列
                                    self.pos_history_x.append(raw_x)
                                    self.pos_history_y.append(raw_y)

                                    # 使用中位数滤波剔除离群噪点帧
                                    median_x = int(np.median(self.pos_history_x))
                                    median_y = int(np.median(self.pos_history_y))

                                    self.last_x, self.last_y = median_x, median_y
                                    self.current_pos = (median_x, median_y)  # 输出过滤后的稳态坐标
                                    self.consecutive_failures = 0
                                    self.found = True

                            elif DEBUG_MODE:
                                log_step("->匹配结果缩放异常，尝试计算其他锚点")
                    else:
                        self.consecutive_failures += 1
                        self.found = False

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
        pil_full_map = Image.fromarray(cv2.cvtColor(self.logic_map_bgr, cv2.COLOR_BGR2RGB))
        # 核心：将系统标点和自定义标点完全分离开，并将主程序自己 (self) 传给大地图
        system_markers = [m for m in self.marker_data if not m.get('is_custom')]
        BigMapWindow(
            self.root,
            pil_full_map,
            system_markers,
            self.custom_markers,
            self.icon_cache,
            self.resource_type_selected_items,
            parent_app=self
        )

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

    def get_marker_by_id(self, marker_id):
        """通过 ID 查找标点"""
        for m in self.marker_data:
            if m['id'] == marker_id:
                return m
        return None

    def load_custom_markers(self):
        """解析自定义点位 JSON，确保类型键统一"""
        custom_points_path = os.path.join(CUSTOM_POINTS_DIR, "user_points.json")
        custom_markers = []
        if os.path.exists(custom_points_path):
            try:
                collected_ids = self.load_picking_data(config.PICKINGDATA_PATH)
                with open(custom_points_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for type_str, point_list in data.items():
                        for pt in point_list:
                            m_id = pt.get('id')
                            lat = pt.get('point', {}).get('lat')
                            lng = pt.get('point', {}).get('lng')
                            mark_type = str(pt.get('markType', '301'))

                            if lat is not None and lng is not None:
                                custom_markers.append({
                                    'id': m_id,
                                    'type': mark_type,  # 统一使用 type 供小地图识别
                                    'markType': mark_type,  # 供编辑功能识别
                                    'pixel_x': lng,
                                    'pixel_y': lat,
                                    'is_collected': m_id in collected_ids,
                                    'is_custom': True
                                })
            except Exception as e:
                print(f"加载自定义标点失败: {e}")
        return custom_markers

    def render_active_route(self, x1, y1, view_w, crop_w, view_h, crop_h, ui_center_x, ui_center_y):
        """增强版：绘制自定义路线，包含从玩家到目标的绝对实线"""
        try:
            if not getattr(self, 'use_custom_route_var', None) or not self.use_custom_route_var.get():
                return
            if not hasattr(self, 'active_route_data') or not self.active_route_data:
                return

            # 过滤掉已被采集（隐藏）的节点
            valid_nodes = []
            for node in self.active_route_data.get('nodes', []):
                marker = self.get_marker_by_id(node['id'])
                if marker and not marker.get('is_collected', False):
                    valid_nodes.append(marker)

            if not valid_nodes:
                return

            # --- 画出玩家当前位置到第一个未采集标点的引导线 ---
            first_m = valid_nodes[0]
            first_rx = int((first_m['pixel_x'] - x1) * (view_w / crop_w))
            first_ry = int((first_m['pixel_y'] - y1) * (view_h / crop_h))

            # 红色实线
            self.canvas.create_line(
                ui_center_x, ui_center_y, first_rx, first_ry,
                fill="#FF00FF", width=3, dash=(4, 2), arrow=tk.LAST, tags="route_line"
            )

            # 绘制后续标点之间的路线连线 (蓝色虚线)
            for i in range(len(valid_nodes) - 1):
                m1 = valid_nodes[i]
                m2 = valid_nodes[i + 1]

                rx1 = int((m1['pixel_x'] - x1) * (view_w / crop_w))
                ry1 = int((m1['pixel_y'] - y1) * (view_h / crop_h))
                rx2 = int((m2['pixel_x'] - x1) * (view_w / crop_w))
                ry2 = int((m2['pixel_y'] - y1) * (view_h / crop_h))

                self.canvas.create_line(
                    rx1, ry1, rx2, ry2,
                    fill="#3399FF", width=3, dash=(4, 2), arrow=tk.LAST, tags="route_line"
                )

        except Exception as e:
            log_step(f"渲染自定义路线发生了错误: {e}")

    def init_big_map_features(self):
        cache_file = config.FEATURES_PATH

        # 构建地图特征池提示
        self.status_label.configure(text="正在构建地图特征池，请稍候...")
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

        self.status_label.configure(text="正在构建地图特征池：计算 ORB 特征点 (耗时较长)...")
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
            self.resource_type_button.configure(text="请选择")
        else:
            text = ", ".join(self.resource_type_selected_items)
            # 如果太长，显示数量
            if len(text) > 20:
                text = f"已选择 {len(self.resource_type_selected_items)} 项"
            self.resource_type_button.configure(text=text)

    def resource_type_close_popup(self):
        if self.resource_type_popup:
            self.resource_type_popup.destroy()
            self.resource_type_popup = None

    def resource_type_get_value(self):
        """获取选中的列表"""
        return self.resource_type_selected_items

    def start_hotkey_listener(self):
        """使用 pynput 监听全局快捷键"""

        def on_activate_l():
            # 必须通过 root.after 调度到主线程执行，防止线程崩溃
            log_step("检测到触发快捷键 ALT+L (鼠标穿透锁定)")
            self.root.after(0, self.hotkey_triggered)

        def on_activate_i():
            # 必须通过 root.after 调度到主线程执行
            log_step("检测到触发快捷键 ALT+I (显示/隐藏面板)")
            self.root.after(0, self.toggle_ctrl_frame)

        # 注意：这里的键名必须是小写，组合用 + 号
        # <alt>+l 代表 Alt + L
        try:
            # 实例化 GlobalHotkeys
            listener = keyboard.GlobalHotKeys({
                '<alt>+l': on_activate_l,
                '<alt>+i': on_activate_i
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
                self.ui_lock_cb.configure(fg="#00FF00") # 锁定状态变绿提醒
            else:
                # 移除穿透属性
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style & ~WS_EX_TRANSPARENT)
                self.ui_lock_cb.configure(fg="white")
        except Exception as e:
            print(f"设置UI锁定失败: {e}")

    def on_route_toggle(self):
        if self.use_custom_route_var.get():
            self.auto_route_planning_var.set(False)  # 互斥：关闭最近路线
            self.load_active_custom_route()

    def on_auto_route_toggle(self):
        if getattr(self, 'auto_route_planning_var', None) and self.auto_route_planning_var.get():
            self.use_custom_route_var.set(False)  # 互斥：关闭自定义路线

    def load_active_custom_route(self):
        """带容错的路线读取"""
        try:
            route_name = self.route_var.get()
            if not route_name:
                return
            route_file = os.path.join(CUSTOM_ROUTES_DIR, route_name)
            if os.path.isfile(route_file):
                with open(route_file, 'r', encoding='utf-8') as f:
                    self.active_route_data = json.load(f)
        except Exception as e:
            log_step(f"加载路线文件失败: {e}")

    def on_custom_marker_added(self, new_marker):
        """实时将大地图的新增标点注入到小地图系统中"""
        self.marker_data.append(new_marker)
        self.custom_markers.append(new_marker)
        self.markers_np_coords = np.array([[m['pixel_x'], m['pixel_y']] for m in self.marker_data], dtype=np.float32)

    def on_custom_marker_deleted(self, marker):
        """实时删除标点"""
        if marker in self.marker_data:
            self.marker_data.remove(marker)
        if marker in self.custom_markers:
            self.custom_markers.remove(marker)
        self.markers_np_coords = np.array([[m['pixel_x'], m['pixel_y']] for m in self.marker_data], dtype=np.float32)

    def refresh_route_list(self):
        """实时刷新路线下拉框"""
        self.available_routes = glob.glob(os.path.join(CUSTOM_ROUTES_DIR, "*.json"))
        route_names = [os.path.basename(r) for r in self.available_routes]
        self.route_combobox.configure(values=route_names)
        if route_names and not self.route_var.get():
            self.route_var.set(route_names[0])

    def on_route_combobox_change(self, choice):
        """当用户在下拉框选择了其他路线时，立刻刷新路线"""
        if self.use_custom_route_var.get():
            self.load_active_custom_route()

    def toggle_ctrl_frame(self):
        """切换控制面板的显示/隐藏状态"""
        self.ui_hidden = not self.ui_hidden
        if self.ui_hidden:
            self.ctrl_frame.pack_forget()  # 隐藏大面板
            self.btn_show_ctrl.pack(side=ctk.BOTTOM, fill=ctk.X, pady=2)  # 显示小按钮
        else:
            self.btn_show_ctrl.pack_forget()  # 隐藏小按钮
            self.ctrl_frame.pack(side=ctk.BOTTOM, fill=ctk.X, padx=5, pady=5)  # 恢复大面板


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
        self.canvas = ctk.CTkCanvas(self, bg='black', highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

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
                minimap = MINIMAP_DATA
                if minimap:
                    self.x = minimap.get("left", 100)
                    self.y = minimap.get("top", 100)
                    self.size = minimap.get("width", 150)
            except Exception as e:
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
        global MINIMAP_DATA
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

        MINIMAP_DATA = {
            "top": real_top,
            "left": real_left,
            "width": real_width,
            "height": real_height
        }

        log_step(f"✅ 坐标锁定成功! 物理位置: ({real_left}, {real_top}) 大小: {real_width}x{real_height}")
        self.destroy()

def log_step(step_name):
    step_info = f"[{time.strftime('%H:%M:%S')}] {step_name}\n"
    print(step_info)
    with open("log.txt", "a", encoding="utf-8") as f:
        f.write(step_info)


def show_welcome_popup(parent):
    popup = ctk.CTkToplevel(parent)
    popup.title(f"LKMT工具，{group_text}")
    target_width = 600
    target_height = 350
    # 保持在最上层
    popup.attributes("-topmost", True)

    # 居中显示
    popup.update_idletasks()
    width = popup.winfo_width()
    height = popup.winfo_height()
    screen_width = popup.winfo_screenwidth()
    screen_height = popup.winfo_screenheight()
    x = (screen_width // 2) - (width // 2)
    y = (screen_height // 2) - (height // 2)
    popup.geometry(f"{target_width}x{target_height}+{x}+{y}")
    popup.deiconify()
    popup.resizable(False, False)

    # ---------------- 占位文本区域 ----------------
    placeholder_text = (
        "欢迎使用LKMT工具\n\n"
        "本工具获取途径（github 夸克网盘）完全免费\n"
        "如果是从他人渠道购买得到下载地址，您这是被骗啦！\n"
        f"{group_text}\n\n"
        "※ 请点击下方按钮或关闭本窗口以继续运行程序。"
    )
    # ---------------------------------------------

    # UI 布局
    frame = ctk.CTkFrame(popup, corner_radius=10)
    frame.pack(fill=ctk.BOTH, expand=True, padx=20, pady=(20, 10))

    lbl = ctk.CTkLabel(
        frame,
        text=placeholder_text,
        justify=tk.LEFT,
        font=("微软雅黑", 17),
        wraplength=target_width - 100
    )
    lbl.pack(fill=ctk.BOTH, expand=True, padx=15, pady=15)

    btn = ctk.CTkButton(
        popup,
        text="我已知晓",
        width=120,
        height=35,
        command=popup.destroy
    )
    btn.pack(pady=20)

    # 阻塞
    popup.grab_set()
    parent.wait_window(popup)

def run_bootstrapper(force_selector=True):
    root = ctk.CTk()
    root.geometry(WINDOW_GEOMETRY)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    log_step("正在显示启动弹窗")
    show_welcome_popup(root)

    # 检查配置是否存在
    log_step("正在检测是否需要小地图选择器")
    needs_selector = not MINIMAP_DATA or force_selector
    if needs_selector:
        log_step("正在创建选择器UI")
        selector_app = MinimapSelector(root)

        # 阻塞Tkinter直到 selector_win 被销毁
        root.wait_window(selector_app)

        log_step("选择器检查完成")

        # 再次检查配置，如果用户直接关了没保存，就退出
        if not os.path.exists(CONFIG_FILE):
            messagebox.showwarning("提示", "未完成校准，程序将退出。")
            root.destroy()
            sys.exit()

    # 重载config
    import importlib
    importlib.reload(config)

    log_step("显示主窗口")
    root.deiconify()  # 重新显示主窗口
    root.update()

    app = MapTrackerApp(root)
    root.mainloop()

class ResourceDownload:
    def __init__(self):
        self.url_point = f"https://wiki.biligame.com/rocom/Data:Mapnew/point.json"



if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    log_step(f"程序启动，欢迎使用LKMT工具，本工具获取途径完全免费，{group_text}")

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