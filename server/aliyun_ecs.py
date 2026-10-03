"""
TS Safe — 阿里云 ECS 云盘快照后端（雏形 / prototype）

防勒索卖点（云端版）：
  - 云盘快照是块级、与实例云盘分离存储；勒索软件加密实例本地文件，并不影响
    已经存在的快照（快照在远端对象存储，不在被加密的云盘上）。
  - 「不可删」机制采用「双凭证」模型：
      * 保护凭证 PROTECTOR（默认启用）：仅有 CreateSnapshot / Describe* 权限，
        没有 DeleteSnapshot。TS Safe 创建的所有快照都用它 —— 因此运行中的
        程序（乃至实例被攻陷拿到 root）都无法删除这些快照。
      * 管理凭证 MANAGER（需单独的 .aliyun_manager.json，默认不加载到进程）：
        拥有完整 ECS 权限，仅在人工/受控清理时显式提供。删除操作若无 MANAGER
        凭证则直接拒绝 —— 这就是「不可删」的硬保证。
  - 保留策略（避免无限增长）：推荐在阿里云控制台配置「自动快照策略」并设置
    保留份数；或配置 MANAGER 凭证后由本程序按 keep 自动清理。

实现：纯标准库实现 ECS ROA（HMAC-SHA1）签名，无需安装任何 SDK，零额外依赖，
可直接在「只装了系统 python3」的 ECS 上跑。

优雅降级：非 ECS 环境（读不到实例元数据）、缺少凭证、网络不可达，全部安全
降级（抛 StorageError 被上游 try 吞掉），绝不阻断主流程。
"""

from __future__ import annotations

import os
import re
import json
import time
import uuid
import datetime
import urllib.request
import urllib.error
import hmac
import hashlib
import base64

from storage import StorageError, Volume, Snapshot


# ---------------------------------------------------------------------------
# 元数据探测（判断是否真在阿里云 ECS 上）
# ---------------------------------------------------------------------------

_META_HOST = "100.100.100.200"     # 阿里云实例元数据服务（VPC 内网固定地址）
_META_TIMEOUT = 1.5

# is_ecs 结果缓存，避免每次探测都打一次元数据（TTL 秒）
_ecs_cache: dict = {"value": None, "ts": 0.0}
_ecs_cache_ttl = 60.0


def _meta_get(path: str) -> "str | None":
    """读取一条实例元数据；任何失败都返回 None。"""
    url = f"http://{_META_HOST}/latest/meta-data/{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_META_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace").strip()
    except Exception:
        return None


def is_ecs() -> bool:
    """当前是否运行在阿里云 ECS 实例内（只读元数据，失败即非 ECS）。结果带缓存。"""
    now = time.monotonic()
    if _ecs_cache["value"] is not None and (now - _ecs_cache["ts"]) < _ecs_cache_ttl:
        return _ecs_cache["value"]  # type: ignore[return-value]
    iid = _meta_get("instance-id")
    val = bool(iid)
    _ecs_cache["value"] = val
    _ecs_cache["ts"] = now
    return val


def instance_metadata() -> dict:
    """尽力探测实例元数据（region / instance-id / zone / instance-type 等）。"""
    out: dict = {}
    for key in ("instance-id", "region-id", "zone-id",
                "instance-type", "image-id", "vpc-id"):
        v = _meta_get(key)
        if v:
            out[key] = v
    return out


# ---------------------------------------------------------------------------
# 凭证加载（双凭证模型）
# ---------------------------------------------------------------------------

def _state_dir() -> str:
    try:
        import storage
        return storage.state_dir()
    except Exception:
        return os.path.join(os.getcwd(), "state")


def _load_cred_pair(env_id: str, env_secret: str,
                    file: "str | None") -> "dict | None":
    """从环境变量或 JSON 文件读一组 AK；缺失返回 None。"""
    ak = os.environ.get(env_id)
    sk = os.environ.get(env_secret)
    if ak and sk:
        return {"id": ak, "secret": sk}
    if file and os.path.exists(file):
        try:
            with open(file, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("access_key_id") and d.get("access_key_secret"):
                return {"id": d["access_key_id"], "secret": d["access_key_secret"]}
        except Exception:
            pass
    return None


def load_credentials() -> dict:
    """返回 {'protector': {...}|None, 'manager': {...}|None, 'region': str}。

    - PROTECTOR：创建/只读专用，无删除权限（防勒索核心）。
    - MANAGER：完整权限，仅在单独的 .aliyun_manager.json 存在时可用；默认不加载
      到运行进程，这是「不可删」的硬保障。
    - 退化兼容：若未单独配置 PROTECTOR，则用通用 NASSAFE_ALIYUN_* 作为合一凭证。
    """
    state = _state_dir()
    protector = _load_cred_pair(
        "NASSAFE_ALIYUN_PROTECTOR_ACCESS_KEY_ID",
        "NASSAFE_ALIYUN_PROTECTOR_ACCESS_KEY_SECRET",
        os.path.join(state, ".aliyun_ecs.json"),
    )
    if protector is None:
        protector = _load_cred_pair(
            "NASSAFE_ALIYUN_ACCESS_KEY_ID",
            "NASSAFE_ALIYUN_ACCESS_KEY_SECRET",
            None,
        )
    manager = _load_cred_pair(
        "NASSAFE_ALIYUN_MANAGER_ACCESS_KEY_ID",
        "NASSAFE_ALIYUN_MANAGER_ACCESS_KEY_SECRET",
        os.path.join(state, ".aliyun_manager.json"),
    )
    region = (os.environ.get("NASSAFE_ALIYUN_REGION")
              or instance_metadata().get("region-id")
              or "cn-hangzhou")
    return {"protector": protector, "manager": manager, "region": region}


def protector_available() -> bool:
    return load_credentials().get("protector") is not None


def manager_available() -> bool:
    """是否存在管理（删除）凭证。默认 False —— 即默认快照不可删。"""
    return load_credentials().get("manager") is not None


# ---------------------------------------------------------------------------
# 极简 ECS ROA 客户端（纯标准库，HMAC-SHA1 签名，零 SDK 依赖）
# ---------------------------------------------------------------------------

def _percent_encode(s: str) -> str:
    """阿里云规范的特殊 percent-encode：保留 A-Za-z0-9-_.~，其余按 %XX（大写），
    空格编码为 %20（不是 +）。"""
    res: list[str] = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if ("A" <= c <= "Z") or ("a" <= c <= "z") or ("0" <= c <= "9") or c in "-_.~":
            res.append(c)
        else:
            res.append("%%%02X" % b)
    return "".join(res)


def _sign(params: dict, secret: str) -> str:
    """按阿里云 ROA 规范生成 HMAC-SHA1 签名。"""
    items = sorted(params.items(), key=lambda kv: kv[0])
    canonical = "&".join(
        f"{_percent_encode(k)}={_percent_encode(str(v))}" for k, v in items
    )
    string_to_sign = (
        "GET&" + _percent_encode("/") + "&" + _percent_encode(canonical)
    )
    key = (secret + "&").encode("utf-8")
    mac = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


def _ecs_request(action: str, params: dict, region: str,
                 cred: dict, timeout: int = 20) -> dict:
    """发送一次 ECS RPC 请求（GET + 查询签名），返回解析后的 JSON。"""
    if not cred or not cred.get("id") or not cred.get("secret"):
        raise StorageError("缺少阿里云凭证")
    allp: dict = {
        "Action": action,
        "Version": "2014-05-26",
        "AccessKeyId": cred["id"],
        "SignatureMethod": "HMAC-SHA1",
        "Timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Format": "JSON",
        "RegionId": region,
    }
    for k, v in (params or {}).items():
        allp[k] = "" if v is None else str(v)
    allp["Signature"] = _sign(allp, cred["secret"])

    query = "&".join(
        f"{_percent_encode(k)}={_percent_encode(str(v))}" for k, v in allp.items()
    )
    url = f"https://ecs.{region}.aliyuncs.com/?" + query
    try:
        req = urllib.request.Request(
            url, method="GET", headers={"User-Agent": "nas-safe/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise StorageError(f"阿里云 API HTTP {exc.code}: {detail[:500]}")
    except Exception as exc:  # 网络不可达 / 超时等
        raise StorageError(f"阿里云 API 请求失败: {exc}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise StorageError(f"阿里云 API 返回非 JSON: {body[:300]}")

    # 错误响应：{"Code": "...", "Message": "...", "RequestId": "..."}
    if "Code" in data and "Message" in data:
        raise StorageError(f"阿里云 API 错误 {data.get('Code')}: {data.get('Message')}")
    return data


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------

def _extract_list(data: dict, outer: str, inner: str) -> list:
    """从 {outer: {inner: [...]}} 或 {outer: [...]} 里抠出列表。"""
    obj = data.get(outer)
    if obj is None:
        return []
    if isinstance(obj, dict):
        lst = obj.get(inner)
    elif isinstance(obj, list):
        lst = obj
    else:
        return []
    return lst if isinstance(lst, list) else []


def _gb_to_bytes(gb) -> "int | None":
    try:
        return int(float(gb) * 1024 ** 3)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _tags_protected(snap: dict) -> bool:
    """快照是否被标记为 TS Safe 受保护（nassafe-vital=true）。"""
    tags = snap.get("Tags")
    if not tags:
        return False
    tag_list = tags.get("Tag") if isinstance(tags, dict) else tags
    if not isinstance(tag_list, list):
        return False
    for t in tag_list:
        if t.get("TagKey") == "nassafe-vital" and t.get("TagValue") == "true":
            return True
    return False


# ---------------------------------------------------------------------------
# 客户端构造
# ---------------------------------------------------------------------------

class EcsClient:
    def __init__(self, region: str, credential: dict) -> None:
        self.region = region
        self.credential = credential

    def request(self, action: str, params: dict, timeout: int = 20) -> dict:
        return _ecs_request(action, params, self.region, self.credential, timeout)


def default_client() -> EcsClient:
    """默认用保护凭证构造客户端（创建/只读）。无保护凭证抛 StorageError。"""
    creds = load_credentials()
    if not creds["protector"]:
        raise StorageError(
            "缺少阿里云保护凭证（NASSAFE_ALIYUN_PROTECTOR_ACCESS_KEY_ID/SECRET "
            "或通用 NASSAFE_ALIYUN_ACCESS_KEY_ID/SECRET）"
        )
    return EcsClient(creds["region"], creds["protector"])


def manager_client() -> "EcsClient | None":
    """构造管理（删除）客户端；默认返回 None（即不可删）。"""
    creds = load_credentials()
    if not creds["manager"]:
        return None
    return EcsClient(creds["region"], creds["manager"])


# ---------------------------------------------------------------------------
# 业务方法（与 qnap.py 同名高层封装，便于 storage 统一调用）
# ---------------------------------------------------------------------------

def list_volumes(client: "EcsClient | None" = None) -> list:
    """列出本实例挂载的云盘，包装成统一 Volume（fs_type=aliyun）。"""
    client = client or default_client()
    instance_id = instance_metadata().get("instance-id") or os.environ.get(
        "NASSAFE_ALIYUN_INSTANCE_ID"
    )
    if not instance_id:
        raise StorageError("无法获取实例 ID（非 ECS 环境，或未设置 NASSAFE_ALIYUN_INSTANCE_ID）")
    data = client.request("DescribeDisks", {
        "RegionId": client.region,
        "InstanceId": instance_id,
        "MaxResults": 100,
    })
    disks = _extract_list(data, "Disks", "Disk")
    out: list = []
    for d in disks:
        did = d.get("DiskId")
        if not did:
            continue
        out.append(Volume(
            name=d.get("DiskName") or did,
            mountpoint=did,           # 用 DiskId 作为查找键
            fs_type="aliyun",
            uuid=did,
            device=d.get("Device") or "",
            volume_id=did,
            backend="aliyun",
            snapshot_dir=None,        # 块级快照，无本地目录
            size_bytes=_gb_to_bytes(d.get("Size")),
        ))
    return out


def list_snapshots(volume: Volume, client: "EcsClient | None" = None) -> list:
    """列出某云盘的快照，包装成统一 Snapshot（fs_type=aliyun）。"""
    client = client or default_client()
    data = client.request("DescribeSnapshots", {
        "RegionId": client.region,
        "DiskId": volume.volume_id or volume.mountpoint,
        "MaxResults": 100,
    })
    snaps = _extract_list(data, "Snapshots", "Snapshot")
    out: list = []
    for s in snaps:
        sid = s.get("SnapshotId")
        if not sid:
            continue
        out.append(Snapshot(
            name=s.get("SnapshotName") or sid,
            volume=volume.volume_id or volume.mountpoint,
            created_at=s.get("CreationTime"),
            snapshot_id=sid,
            size_bytes=_gb_to_bytes(s.get("SnapshotSize")),
            readonly=True,
            fs_type="aliyun",
            backend="aliyun",
            vital=_tags_protected(s),
            status=s.get("Status"),
            description=s.get("Description") or "",
        ))
    return out


def create_snapshot(volume: Volume, name: str, vital: bool = True,
                   client: "EcsClient | None" = None) -> Snapshot:
    """创建云盘快照（用保护凭证，无删除权限）。

    - 永久保留（手动快照本就不过期）。
    - 打标签 nassafe=protected / nassafe-vital=true，便于识别与防删判定。
    """
    if not re.match(r"^[A-Za-z0-9_\-]+$", name):
        raise StorageError("快照名只允许字母数字下划线横线")
    client = client or default_client()
    data = client.request("CreateSnapshot", {
        "RegionId": client.region,
        "DiskId": volume.volume_id or volume.mountpoint,
        "SnapshotName": name,
        "Description": "TS Safe 受保护快照 (anti-ransomware)",
        "Tag.1.Key": "nassafe",
        "Tag.1.Value": "protected",
        "Tag.2.Key": "nassafe-vital",
        "Tag.2.Value": "true" if vital else "false",
    })
    sid = data.get("SnapshotId")
    if not sid:
        raise StorageError(f"创建快照未返回 SnapshotId: {data}")
    return Snapshot(
        name=name,
        volume=volume.volume_id or volume.mountpoint,
        created_at=_now_iso(),
        snapshot_id=sid,
        readonly=True,
        fs_type="aliyun",
        backend="aliyun",
        vital=vital,
        status="Creating",
        description="TS Safe 受保护快照",
    )


def delete_snapshot(snapshot: Snapshot, client: "EcsClient | None" = None) -> None:
    """删除快照 —— 硬要求管理凭证（双凭证防删模型）。

    若未提供管理凭证（默认运行进程不持有），直接拒绝，这就是「不可删」保证。
    """
    client = client or manager_client()
    if client is None:
        raise StorageError(
            "快照受保护：删除需要管理凭证（MANAGER）。默认运行的进程不持有删除"
            "权限，这是防勒索设计 —— 请在 .aliyun_manager.json 配置管理 AK 后重试，"
            "或到阿里云控制台手动删除。"
        )
    client.request("DeleteSnapshot", {
        "RegionId": client.region,
        "SnapshotId": snapshot.snapshot_id,
    })


def create_recovery_disk(snapshot: Snapshot, client: "EcsClient | None" = None) -> dict:
    """从快照创建一块新云盘（恢复用）。

    块级快照无法直接浏览文件；恢复单文件的标准路径是：
      1) 用本方法从快照创建云盘；2) 挂载到实例；3) 从挂载点拷贝文件。
    这是真实的「取回」通道（需手动挂载，或后续接自动挂载脚本）。
    """
    client = client or default_client()
    data = client.request("CreateDisk", {
        "RegionId": client.region,
        "SnapshotId": snapshot.snapshot_id,
        "DiskName": f"nassafe-recover-{snapshot.snapshot_id}",
    })
    did = data.get("DiskId")
    return {
        "ok": True,
        "disk_id": did,
        "snapshot_id": snapshot.snapshot_id,
        "message": (
            "已从快照创建恢复云盘，请将其 Attach 到实例并挂载后浏览/取回文件；"
            "完成后请记得 Detach 并 DeleteDisk 避免持续计费。"
        ),
    }


# ---------------------------------------------------------------------------
# 统一浏览 / 取回入口（storage.browse_snapshot / restore_from_snapshot 会调用）
# ---------------------------------------------------------------------------

def browse_snapshot(snapshot: Snapshot, subpath: str = "") -> dict:
    """云盘快照为块级，无法直接在线浏览文件。返回可读说明 + 恢复通道提示。"""
    return {
        "ok": False,
        "backend": "aliyun",
        "message": (
            "阿里云云盘快照为块级，无法直接在线浏览文件。需要时请调用"
            " aliyun_ecs.create_recovery_disk() 从快照创建云盘，挂载到实例后再浏览；"
            "或到阿里云控制台「快照 → 创建云盘」完成恢复。"
        ),
        "snapshot_id": snapshot.snapshot_id,
        "subpath": subpath,
    }


def restore_from_snapshot(snapshot: Snapshot, rel_path: str, dest: str) -> dict:
    """云盘快照为块级，无法直接取回单文件。返回可读说明。"""
    raise StorageError(
        "阿里云云盘快照为块级，无法直接从快照取回单文件。恢复路径："
        "用 create_recovery_disk() 从快照创建云盘 → 挂载到实例 → 从挂载点拷贝文件。"
    )
