import re
import json
from html import unescape


def extract_map_points_v2(input_file, output_file):
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. 定位 mapPointData 区域
    pattern = re.compile(r'<div[^>]*id="mapPointData"[^>]*>(.*?)</div>', re.DOTALL)
    match = pattern.search(content)

    if not match:
        print("未找到 id='mapPointData' 的数据区域")
        return

    raw_data = match.group(1)

    # 2. 初步清理：移除 HTML 标签并还原转义字符
    # 注意：我们先做 unescape，再通过正则把所有 <a> 标签去掉，只留下里面的文本
    clean_text = unescape(raw_data)
    clean_text = re.sub(r'<[^>]+>', '', clean_text)

    # 3. 关键修复：修复非标准 JSON 值
    # 针对你提到的 50002:Data:Mapnew/... 这种情况
    # 我们把冒号后面紧跟着 "Data:..." 路径的非法值替换为 null
    clean_text = re.compile(r':\s*Data:[\w/.]+').sub(': null', clean_text)

    # 4. 修复 Key 没有双引号的问题
    # 将 { 201: 变成 { "201":
    clean_text = re.sub(r'([{,]\s*)(\d+)(\s*:)', r'\1"\2"\3', clean_text)

    # 5. 尝试解析
    try:
        # 移除末尾可能多余的逗号（如果存在）
        clean_text = clean_text.strip().rstrip(',')

        data = json.loads(clean_text)

        with open(output_file, 'w', encoding='utf-8') as f_out:
            json.dump(data, f_out, ensure_ascii=False, indent=4)

        print(f"提取成功！已过滤非法链接，数据已保存至: {output_file}")

    except json.JSONDecodeError as e:
        print(f"解析失败: {e}")
        # 打印错误点附近内容辅助诊断
        pos = e.pos
        print("错误点附近内容：", clean_text[max(0, pos - 50):pos + 50])


# 执行
extract_map_points_v2('point.json', 'point_cleaned.json')