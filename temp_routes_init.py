import json
import os
import math

# ================= 配置区 =================
# 原数据 points.json 的路径 (请根据你的实际情况修改，可能是 'points.json' 或 'assets/points.json')
POINTS_JSON_PATH = "assest/points.json"
# 生成的路线保存目录
OUTPUT_DIR = "assest/custom/routes"


# ==========================================

def calculate_distance(p1, p2):
    """计算两点之间的欧几里得平方距离 (无需开根号，比较大小足够了，提升速度)"""
    return (p1['lng'] - p2['lng']) ** 2 + (p1['lat'] - p2['lat']) ** 2


def generate_greedy_route(points_data, start_id, min_type, max_type, output_filename):
    """
    使用贪心算法生成最短路线
    """
    # 1. 从源数据中筛选符合条件的点位
    filtered_points = []

    for mark_type_str, points_list in points_data.items():
        if not mark_type_str.isdigit():
            continue

        mark_type = int(mark_type_str)
        # 匹配指定的类型范围
        if min_type <= mark_type <= max_type:
            # 兼容有些 json 节点是 None 的情况
            if not isinstance(points_list, list):
                continue

            for pt in points_list:
                if 'point' in pt and 'lat' in pt['point'] and 'lng' in pt['point']:
                    filtered_points.append({
                        'id': pt['id'],
                        'markType': mark_type,
                        'lat': pt['point']['lat'],
                        'lng': pt['point']['lng']
                    })

    if not filtered_points:
        print(f"[{output_filename}] ❌ 失败：未找到 markType 在 {min_type}-{max_type} 之间的标点！")
        return

    # 2. 查找指定的起点
    start_point = None
    for pt in filtered_points:
        if pt['id'] == start_id:
            start_point = pt
            break

    if not start_point:
        print(f"[{output_filename}] ⚠️ 警告：未找到指定的起点 ID '{start_id}'，将默认使用该分类的第一个点作为起点。")
        start_point = filtered_points[0]

    # 3. 核心：最近邻贪心算法连线
    unvisited = [pt for pt in filtered_points if pt['id'] != start_point['id']]
    current_point = start_point
    route_sequence = [current_point]

    while unvisited:
        # 寻找距离 current_point 最近的点
        nearest_point = min(unvisited, key=lambda p: calculate_distance(current_point, p))
        route_sequence.append(nearest_point)
        current_point = nearest_point
        unvisited.remove(nearest_point)

    # 4. 格式化为软件所需的数据结构
    route_name = output_filename.replace(".json", "")
    route_data = {
        "version": "1.0",
        "route_name": route_name,
        "node_count": len(route_sequence),
        "nodes": []
    }

    for idx, pt in enumerate(route_sequence):
        route_data["nodes"].append({
            "order": idx,
            "id": pt['id'],
            "is_custom": False  # 基础点位均视为非 custom 标点
        })

    # 5. 保存文件
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, output_filename)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(route_data, f, ensure_ascii=False, indent=2)

    print(f"[{output_filename}] ✅ 生成成功！共连接了 {len(route_sequence)} 个标点，已保存至 {out_path}。")


def main():
    if not os.path.exists(POINTS_JSON_PATH):
        print(f"❌ 找不到标点数据文件: {POINTS_JSON_PATH}")
        print("请打开脚本，修改 POINTS_JSON_PATH 为你程序中实际的 points.json 路径。")
        return

    # 读取原始点位数据
    try:
        with open(POINTS_JSON_PATH, 'r', encoding='utf-8') as f:
            points_data = json.load(f)
    except Exception as e:
        print(f"❌ 解析 JSON 失败: {e}")
        return

    print("开始生成路线，请稍候...\n" + "-" * 40)

    # 任务 1：全采集 (701-737)
    generate_greedy_route(
        points_data=points_data,
        start_id="15rl142mq1x",
        min_type=701, max_type=737,
        output_filename="预设-全采集.json"
    )

    # 任务 2：全宝箱 (301-322)
    generate_greedy_route(
        points_data=points_data,
        start_id="1memzvqb3gk",
        min_type=301, max_type=322,
        output_filename="预设-全宝箱.json"
    )

    # 任务 3：全眠枭之星 (802-803)
    generate_greedy_route(
        points_data=points_data,
        start_id="1i2o6o6lxtg",
        min_type=802, max_type=803,
        output_filename="预设-全眠枭之星.json"
    )

    print("-" * 40 + "\n全部生成完毕！现在你可以打开软件在列表中选择这些路线了。")


if __name__ == "__main__":
    main()