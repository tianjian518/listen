"""
天翼云盘 (cloud.189.cn) 网页版接口封装

登录链路（已实测可用）：
    COOKIE_LOGIN_USER  ->  /v2/getUserBriefInfo.action  ->  sessionKey
    sessionKey         ->  /api/open/oauth2/getAccessTokenBySsKey.action  ->  accessToken

上传走网页版分片协议：
    initMultiUpload -> getMultiUploadUrls -> PUT 分片 -> commitMultiUploadFile

说明：走的是网页版内部接口，非官方公开 API，官方改版后可能失效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import os
import re
import time
import uuid
from urllib.parse import quote, unquote

import httpx
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

WEB = "https://cloud.189.cn"
API = "https://api.cloud.189.cn"
UPLOAD = "https://upload.cloud.189.cn"

APP_ID = "8025431004"
WEB_APP_KEY = "600100422"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SLICE_SIZE = 10 * 1024 * 1024  # 10MB 一片


class TianyiError(Exception):
    """云盘操作失败，message 直接给用户看"""


def _aes_ecb(data: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).encrypt(pad(data, AES.block_size))


def _rsa_b64(text: str, pub_key_b64: str) -> str:
    pem = f"-----BEGIN PUBLIC KEY-----\n{pub_key_b64}\n-----END PUBLIC KEY-----"
    enc = PKCS1_v1_5.new(RSA.import_key(pem)).encrypt(text.encode())
    return base64.b64encode(enc).decode()


def _hmac_sha1(message: str, secret: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha1).hexdigest()


def _plain_sign(params: dict) -> str:
    items = sorted(f"{k}={v}" for k, v in params.items())
    return hashlib.md5("&".join(items).encode()).hexdigest()


def _parse_xml_int(text: str, tag: str) -> int:
    m = re.search(rf"<{tag}>(-?\d+)</{tag}>", text)
    return int(m.group(1)) if m else 0


class TianyiClient:
    def __init__(self, cookie_login_user: str):
        self.cookie = (cookie_login_user or "").strip()
        if self.cookie.startswith("COOKIE_LOGIN_USER="):
            self.cookie = self.cookie.split("=", 1)[1]
        self.session_key = ""
        self.access_token = ""
        self.account = ""
        self.nickname = ""
        self._rsa = {"expire": 0, "pkid": "", "pub": ""}
        self.http = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=20.0),
            follow_redirects=True,
            headers={
                "User-Agent": UA,
                "Accept": "application/json;charset=UTF-8",
                "Referer": "https://cloud.189.cn/",
                "Cookie": f"COOKIE_LOGIN_USER={self.cookie}" if self.cookie else "",
            },
        )

    # ---------- 基础请求 ----------

    def _web_get(self, url: str, params: dict | None = None,
                 signed: bool = False) -> httpx.Response:
        q = dict(params or {})
        headers: dict = {}
        if signed:
            ts = str(int(time.time() * 1000))
            q["Timestamp"] = ts
            q["AppKey"] = WEB_APP_KEY
            sig = _plain_sign(q)
            headers.update({
                "Sign-Type": "1", "Signature": sig,
                "Timestamp": ts, "AppKey": WEB_APP_KEY,
            })
        if self.session_key and "sessionKey" not in q:
            q["sessionKey"] = self.session_key
        return self.http.get(url, params=q, headers=headers)

    def _web_post(self, url: str, params: dict | None = None,
                  data: dict | None = None) -> httpx.Response:
        q = dict(params or {})
        if self.session_key and "sessionKey" not in q:
            q["sessionKey"] = self.session_key
        return self.http.post(url, params=q, data=data)

    # ---------- 登录 ----------

    def connect(self) -> dict:
        if not self.cookie:
            raise TianyiError("还没有登录，请先扫码")

        r = self._web_get(f"{WEB}/v2/getUserBriefInfo.action")
        try:
            d = r.json()
        except Exception as e:
            raise TianyiError("云盘连接异常，请重新扫码") from e

        if d.get("res_code") != 0 or not d.get("sessionKey"):
            raise TianyiError("登录已失效，请重新扫码（右上角）")

        self.session_key = d["sessionKey"]
        self.account = d.get("userAccount", "")
        self.nickname = d.get("nickname", "")
        self._fetch_access_token()
        return self.space_info()

    def _fetch_access_token(self):
        try:
            r = self._web_get(
                f"{WEB}/api/open/oauth2/getAccessTokenBySsKey.action",
                {"sessionKey": self.session_key},
            )
            self.access_token = r.json().get("accessToken") or ""
        except Exception:
            self.access_token = ""

    # ---------- 容量 ----------

    def space_info(self) -> dict:
        r = self._web_get(f"{WEB}/api/portal/getUserSizeInfo.action")
        total = used = free = 0
        if r.text.lstrip().startswith("{"):
            d = r.json()
            cap = d.get("cloudCapacityInfo") or {}
            total = int(cap.get("totalSize", 0) or 0)
            used = int(cap.get("usedSize", 0) or 0)
            free = int(cap.get("freeSize", 0) or 0)
        elif "<totalSize>" in r.text:
            total = _parse_xml_int(r.text, "totalSize")
            used = _parse_xml_int(r.text, "usedSize")
            free = _parse_xml_int(r.text, "freeSize")
        if not total:
            raise TianyiError("读取容量失败，登录可能已过期")
        return {
            "logged_in": True,
            "account": self.account,
            "nickname": self.nickname,
            "total": total,
            "used": used,
            "free": free or max(total - used, 0),
        }

    # ---------- 目录 ----------

    def list_folders(self, folder_id: str = "-11") -> dict:
        r = self._web_get(
            f"{WEB}/api/open/file/listFiles.action",
            {
                "folderId": str(folder_id),
                "pageSize": "200",
                "pageNum": "1",
                "mediaType": "0",
                "iconOption": "5",
                "orderBy": "lastOpTime",
                "descending": "false",
            },
            signed=True,
        )
        d = r.json()
        if d.get("res_code") != 0:
            raise TianyiError(f"读取目录失败：{d.get('res_message', d)}")

        ao = d.get("fileListAO") or {}
        folders = [
            {"id": str(f.get("id")), "name": f.get("name"), "is_folder": True}
            for f in (ao.get("folderList") or [])
        ]
        files = [
            {
                "id": str(f.get("id")),
                "name": f.get("name"),
                "size": int(f.get("size", 0) or 0),
                "is_folder": False,
            }
            for f in (ao.get("fileList") or [])
        ]
        return {"folders": folders, "files": files, "parent": str(folder_id)}

    def list_page(self, folder_id: str = "-11", page_num: int = 1,
                  page_size: int = 200) -> dict:
        """单页列举（听书用：大目录要循环翻页）。

        fileListSize 是该目录下的文件总数，翻页时据此判断是否取完；
        folderList 不分页，第一页就返回全部子文件夹。
        """
        r = self._web_get(
            f"{WEB}/api/open/file/listFiles.action",
            {
                "folderId": str(folder_id),
                "pageSize": str(page_size),
                "pageNum": str(page_num),
                "mediaType": "0",
                "iconOption": "5",
                "orderBy": "lastOpTime",
                "descending": "false",
            },
            signed=True,
        )
        d = r.json()
        if d.get("res_code") != 0:
            raise TianyiError(f"读取目录失败：{d.get('res_message', d)}")

        ao = d.get("fileListAO") or {}
        folders = [
            {
                "id": str(f.get("id")),
                "name": f.get("name", ""),
                "is_folder": True,
                "mtime": f.get("lastOpTime", ""),
            }
            for f in (ao.get("folderList") or [])
        ]
        files = [
            {
                "id": str(f.get("id")),
                "name": f.get("name", ""),
                "size": int(f.get("size", 0) or 0),
                "mtime": f.get("lastOpTime", "") or f.get("createDate", ""),
                "is_folder": False,
            }
            for f in (ao.get("fileList") or [])
        ]
        # 注意：fileListSize 是「本页返回的条数」，count 才是该目录的文件总数。
        # 取错字段会以为一页就是全部（469 集只显示 200 集）。
        total = int(ao.get("count") or 0)
        return {
            "folders": folders,
            "files": files,
            "total": total,
            "returned": len(files),
        }

    def find_folder(self, name: str, parent_id: str = "-11") -> str:
        """按名字找子文件夹，找不到返回空串（不会创建）"""
        try:
            listing = self.list_page(parent_id, 1, 200)
        except TianyiError:
            return ""
        for f in listing["folders"]:
            if f["name"].strip() == name.strip():
                return f["id"]
        return ""

    def create_folder(self, parent_id: str, name: str) -> str:
        r = self._web_post(
            f"{WEB}/api/open/file/createFolder.action",
            data={"parentFolderId": str(parent_id), "folderName": name},
        )
        d = r.json()
        if d.get("res_code") != 0:
            raise TianyiError(f"建文件夹失败：{d.get('res_message', d)}")
        return str(d.get("id", ""))

    def ensure_folder(self, path: str, root_id: str = "-11") -> str:
        cur = root_id
        for part in [p for p in path.split("/") if p.strip()]:
            part = part.strip()
            listing = self.list_folders(cur)
            hit = next((f for f in listing["folders"] if f["name"] == part), None)
            cur = hit["id"] if hit else self.create_folder(cur, part)
        return cur

    # ---------- 直链（302 播放用） ----------

    def get_download_url(self, file_id: str) -> str:
        r = self._web_get(
            f"{WEB}/api/open/file/getFileDownloadUrl.action",
            {"fileId": str(file_id)},
        )
        d = r.json()
        if d.get("res_code") != 0 or not d.get("fileDownloadUrl"):
            raise TianyiError(f"获取播放直链失败：{d.get('res_message', d)}")
        return d["fileDownloadUrl"]

    def delete_files(self, file_ids: list[str]) -> bool:
        """删除文件或文件夹（天翼没有批量接口，逐个删）"""
        ids = [str(i) for i in (file_ids or []) if i]
        for fid in ids:
            r = self._web_post(
                f"{WEB}/api/open/file/deleteFile.action",
                data={"fileId": fid},
            )
            try:
                d = r.json()
            except Exception as e:
                raise TianyiError("删除请求异常，请稍后再试") from e
            if d.get("res_code") != 0:
                raise TianyiError(f"删除失败：{d.get('res_message', d)}")
        return True

    def rename(self, file_id: str, new_name: str) -> bool:
        """重命名云盘里的文件（保留原扩展名由调用方负责）"""
        r = self._web_get(
            f"{WEB}/api/open/file/renameFile.action",
            {"fileId": str(file_id), "destFileName": new_name},
        )
        try:
            d = r.json()
        except Exception as e:
            raise TianyiError("重命名请求异常，请稍后再试") from e
        if d.get("res_code") != 0:
            raise TianyiError(f"重命名失败：{d.get('res_message', d)}")
        return True

    # ---------- 上传 ----------

    def _get_rsa_key(self):
        now = int(time.time() * 1000)
        if self._rsa["expire"] > now and self._rsa["pub"]:
            return self._rsa["pub"], self._rsa["pkid"]
        r = self.http.get(f"{API}/security/generateRsaKey.action")
        d = r.json()
        if d.get("res_code") != 0:
            raise TianyiError("获取加密公钥失败，稍后再试")
        self._rsa = {
            "pub": d.get("pubKey", ""),
            "pkid": d.get("pkId", ""),
            "expire": int(d.get("expire", 0) or 0),
        }
        return self._rsa["pub"], self._rsa["pkid"]

    def _upload_request(self, uri: str, form: dict) -> dict:
        date = str(int(time.time() * 1000))
        req_id = str(uuid.uuid4())
        rand_key = uuid.uuid4().hex[:16]

        form_str = "&".join(f"{k}={v}" for k, v in form.items())
        params_hex = _aes_ecb(form_str.encode(), rand_key.encode()).hex()

        msg = (
            f"SessionKey={self.session_key}&Operate=GET&RequestURI={uri}"
            f"&Date={date}&params={params_hex}"
        )
        pub, pkid = self._get_rsa_key()
        r = self.http.get(
            f"{UPLOAD}{uri}?params={params_hex}",
            headers={
                "accept": "application/json;charset=UTF-8",
                "SessionKey": self.session_key,
                "Signature": _hmac_sha1(msg, rand_key),
                "X-Request-Date": date,
                "X-Request-ID": req_id,
                "EncryptionText": _rsa_b64(rand_key, pub),
                "PkId": pkid,
            },
        )
        try:
            data = r.json()
        except Exception as e:
            raise TianyiError(f"上传接口返回异常（HTTP {r.status_code}）") from e

        if data.get("code") != "SUCCESS":
            raise TianyiError(f"上传失败：{data.get('msg') or data}")
        return data

    def upload(self, local_path: str, folder_id: str, display_name: str | None = None,
               progress_cb=None) -> str:
        size = os.path.getsize(local_path)
        name = display_name or os.path.basename(local_path)
        total_slices = max(1, math.ceil(size / SLICE_SIZE))

        init = self._upload_request(
            "/person/initMultiUpload",
            {
                "parentFolderId": str(folder_id),
                "fileName": quote(name),
                "fileSize": str(size),
                "sliceSize": str(SLICE_SIZE),
                "lazyCheck": "1",
            },
        )
        upload_file_id = init["data"]["uploadFileId"]

        md5_list = []
        whole = hashlib.md5()
        done = 0

        with open(local_path, "rb") as fh:
            for idx in range(1, total_slices + 1):
                chunk = fh.read(SLICE_SIZE)
                done += len(chunk)
                md5_bytes = hashlib.md5(chunk).digest()
                md5_list.append(md5_bytes.hex().upper())
                whole.update(chunk)

                slot = self._upload_request(
                    "/person/getMultiUploadUrls",
                    {
                        "partInfo": f"{idx}-{base64.b64encode(md5_bytes).decode()}",
                        "uploadFileId": upload_file_id,
                    },
                )
                part = slot["uploadUrls"][f"partNumber_{idx}"]
                raw_header = unquote(part["requestHeader"])
                headers = {
                    kv.split("=", 1)[0]: kv.split("=", 1)[1]
                    for kv in raw_header.split("&")
                    if "=" in kv
                }
                put = httpx.put(part["requestURL"], headers=headers,
                                content=chunk, timeout=300.0)
                if put.status_code >= 400:
                    raise TianyiError(
                        f"分片 {idx}/{total_slices} 上传失败（HTTP {put.status_code}）"
                    )

                if progress_cb:
                    progress_cb(min(done / size, 1.0))

        file_md5 = whole.hexdigest()
        slice_md5 = file_md5 if size <= SLICE_SIZE else \
            hashlib.md5("\n".join(md5_list).encode()).hexdigest()

        result = self._upload_request(
            "/person/commitMultiUploadFile",
            {
                "uploadFileId": upload_file_id,
                "fileMd5": file_md5,
                "sliceMd5": slice_md5,
                "lazyCheck": "1",
                "opertype": "3",
            },
        )
        if result.get("code") != "SUCCESS":
            raise TianyiError(f"上传收尾失败：{result.get('msg')}")

        f = result.get("file") or {}
        return str(f.get("userFileId") or f.get("id") or "")

    def close(self):
        try:
            self.http.close()
        except Exception:
            pass
