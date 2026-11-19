"""把已核对的功能块内正交线转成嘉立创专业版原理图的 WIRE、LINE 和 ATTR 候选记录。"""

import hashlib
import math

from schematic_wiring import native_name_seeds


def model_attributes_for_move(records, original_netlist, moved_ids):
    """从现场基线网表补齐跨页实例可能失去的三个模型属性，仅返回需要新增的隐藏 ATTR 记录。

    records 为尚未写回的目标页 (header, body)，moved_ids 为要保护的原生器件 ID。
    不推断模型或封装。身份、已有属性与基线冲突、重复属性或 ID 冲突均抛出 ValueError。
    """
    model_keys = ("3D Model", "3D Model Title", "3D Model Transform")
    ids = {h.get("id") for h, b in records}
    parts = {h["id"] for h, b in records if h["type"] == "COMPONENT"}
    moved = set(moved_ids)
    components = original_netlist["components"]
    if moved - parts or moved - components.keys():
        raise ValueError("跨页器件必须同时出现在目标图页和基线网表中")
    inherited = {}
    for h, b in records:
        pair = b.get("parentId"), b.get("key")
        if h["type"] == "ATTR" and pair[0] in moved and pair[1] in model_keys:
            if pair in inherited:
                raise ValueError("目标页存在重复模型属性")
            inherited[pair] = b.get("value")
    extra = []
    for uid in sorted(moved):
        props = components[uid]["props"]
        if props.get("Unique ID") != uid:
            raise ValueError("原生网表器件身份不一致")
        for key in model_keys:
            pair = uid, key
            if pair in inherited:
                if key not in props or props[key] != inherited[pair]:
                    raise ValueError("目标原有模型属性与基线不一致")
            elif key in props:
                aid = hashlib.sha256(f"3d/{uid}/{key}".encode("utf-8")).hexdigest()[:16]
                if aid in ids:
                    raise ValueError("属性 ID 冲突")
                ids.add(aid)
                extra.append(
                    (
                        {"type": "ATTR", "id": aid},
                        {
                            "parentId": uid,
                            "key": key,
                            "value": props[key],
                            "x": None,
                            "y": None,
                            "rotation": None,
                            "keyVisible": False,
                            "valueVisible": False,
                        },
                    )
                )
    return extra


def preserve_channel_ids(records, original_netlist, moved_ids):
    """按基线网表保存跨页器件的 Channel ID，返回新的目标页记录列表。

    输入为尚未写回的原生图页、现场原生网表和迁移 UID。原来为空的属性复用原 ATTR
    及其全部元数据，仅补入源网表导出的值。目标页缺少属性时才新增隐藏 ATTR。
    UID、非空值、重复属性或 ID 冲突引发 ValueError，输入记录不变。
    """
    moved = set(moved_ids)
    parts = {header["id"] for header, body in records if header["type"] == "COMPONENT"}
    ids = {header["id"] for header, body in records if "id" in header}
    components = original_netlist["components"]
    if moved - parts or moved - components.keys():
        raise ValueError("跨页器件必须出现在目标页以及原生基线网表")
    values = {}
    for uid in moved:
        props = components[uid]["props"]
        if props.get("Unique ID") != uid:
            raise ValueError("跨页器件身份与基线网表不一致")
        value = props.get("Channel ID")
        if value is not None and not isinstance(value, str):
            raise ValueError("基线 Channel ID 类型无效")
        values[uid] = value

    output = []
    seen = set()
    for header, body in records:
        uid = body.get("parentId")
        if (
            header["type"] == "ATTR"
            and body.get("key") == "Channel ID"
            and uid in moved
        ):
            if uid in seen:
                raise ValueError("同一器件有多个 Channel ID 属性")
            seen.add(uid)
            expected = values[uid]
            existing = body.get("value")
            if existing not in (None, "", expected):
                raise ValueError("目标 Channel ID 与基线不符")
            if existing in (None, "") and expected:
                body = {**body, "value": expected}
        output.append((header, body))
    for uid in sorted(moved - seen):
        if not values[uid]:
            continue
        aid = hashlib.sha256(f"channel/{uid}".encode("utf-8")).hexdigest()[:16]
        if aid in ids:
            raise ValueError("新增 Channel ID 属性的原生 ID 冲突")
        ids.add(aid)
        output.append(
            (
                {"type": "ATTR", "id": aid},
                {
                    "parentId": uid,
                    "key": "Channel ID",
                    "value": values[uid],
                    "x": None,
                    "y": None,
                    "rotation": None,
                    "keyVisible": False,
                    "valueVisible": False,
                },
            )
        )
    return output


def wire_records(
    page, wires, existing_ids, *, project_uuid, page_uuid, source_fingerprint, measure=None
):
    """入参为真实图页、路由折线、已用图元 ID 及来源标识。输出有序的 (header, body) 列表。

    每棵线树的所有 LINE 共用一个原生 WIRE，NET 属性只归属该 WIRE。源文件纵轴和编辑器相反。
    可选 measure(text, size) 返回与排字时相同的字宽、字高，用来核验已有网名坐标。
    遇到身份缺失、未布通、文字冲突或 ID 冲突时抛出 ValueError。函数只生成新线记录，
    整页的票号、原有对象的保留与替换、写前备份及编辑器读回属于上层合并与写回步骤。
    """
    if not all(
        isinstance(v, str) and v.strip()
        for v in (project_uuid, page_uuid, source_fingerprint)
    ):
        raise ValueError("生成原生图元需要工程、页和原生源指纹")
    if page.get("uuid") is not None and page["uuid"] != page_uuid:
        raise ValueError("图页 UUID 不匹配")
    result = native_name_seeds(page, wires, measure=measure)
    if result["label_conflicts"]:
        raise ValueError(f"原生网名位置存在冲突，需复核排字位置或实际字体尺寸：{result['label_conflicts']}")
    used = set(existing_ids)
    records = []

    def append(kind, body):
        key = f"{project_uuid}/{page_uuid}/{source_fingerprint}/{len(records)}/{kind}"
        primitive_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        if primitive_id in used:
            raise ValueError("生成的图元 ID 与已保留图元冲突")
        used.add(primitive_id)
        records.append(({"type": kind, "id": primitive_id}, body))
        return primitive_id

    for tree in result["wire_trees"]:
        owner = append("WIRE", {})
        members = [result["wires"][index] for index in tree]
        (named,) = (w for w in members if w["explicit_net"])
        for member in members:
            points = member["points"]
            for (x0, y0), (x1, y1) in zip(points, points[1:]):
                append(
                    "LINE",
                    {
                        "startX": x0,
                        "startY": -y0,
                        "endX": x1,
                        "endY": -y1,
                        "strokeWidth": 1,
                        "strokeColor": None,
                        "fillColor": None,
                        "strokeStyle": None,
                        "fillStyle": None,
                        "lineGroup": owner,
                    },
                )
        base = dict(
            parentId=owner,
            x=None,
            y=None,
            rotation=None,
            color=None,
            fontFamily=None,
            fontSize=None,
            fontWeight=None,
            italic=None,
            underline=None,
            align=None,
            keyVisible=None,
            valueVisible=None,
            fillColor=None,
        )
        append("ATTR", dict(base, key="Relevance", value="[]"))
        x, y = named["label"]
        style = named.get("label_style", {})
        size = style.get("size", 7)
        if not math.isfinite(size) or size <= 0:
            raise ValueError("网名字号必须是有限正数")
        append(
            "ATTR",
            dict(
                base,
                key="NET",
                value=named["net"],
                keyVisible=False,
                valueVisible=True,
                x=x,
                y=-y,
                rotation=0,
                fontSize=size,
                align=style.get("align", "CENTER_MIDDLE"),
            ),
        )
    return records
