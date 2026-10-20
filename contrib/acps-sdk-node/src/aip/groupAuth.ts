/**
 * 群组 ACL 客户端（对应 Python `acps_sdk.aip.aip_group_auth`）
 *
 * 调用 mq-auth-server 的群组成员 ACL 接口：
 * - `PUT    /groups/{leader}/{group}/members/{member}`            加入成员
 * - `DELETE /groups/{leader}/{group}/members/{member}`            移除成员
 * - `DELETE /groups/{leader}/{group}`                             删除群组
 * - `DELETE /groups/{leader}/{group}/members/{member}/connection` 关闭成员连接
 */

import { STATUS_CODES } from "node:http";

import {
  type HttpResponseData,
  type HttpTransport,
  type TlsClientOptions,
  createNodeTransport,
} from "../internal/http.js";
import { getLogger } from "../internal/logger.js";
import { pyStrip } from "../internal/pyCompat.js";
import { pyQuote, redactUrl } from "./groupRuntime.js";

const logger = getLogger("acps_sdk.aip.aip_group_auth");

export class GroupAclError extends Error {
  override name = "GroupAclError";
}

export interface GroupAclClientOptions {
  baseUrl: string;
  /** 对应 Python `ssl_context`：HTTPS 校验服务端用的 CA，以及（可选）mTLS 客户端证书 */
  sslContext?: TlsClientOptions | null;
  /** 超时（秒），默认 10.0 */
  timeout?: number;
  /** 测试注入用的传输层（对应 `httpx.MockTransport`） */
  transport?: HttpTransport;
}

export interface GroupMemberRef {
  leaderAic: string;
  groupId: string;
  memberAic: string;
}

export class GroupAclClient {
  readonly baseUrl: string;
  private readonly transport: HttpTransport;
  private readonly timeoutMs: number;

  constructor({ baseUrl, sslContext = null, timeout = 10.0, transport }: GroupAclClientOptions) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.timeoutMs = timeout * 1000;
    this.transport = transport ?? createNodeTransport(sslContext ?? undefined);
  }

  async addMember({ leaderAic, groupId, memberAic }: GroupMemberRef): Promise<void> {
    await this._request(
      "PUT",
      `/groups/${pyQuote(leaderAic)}/${pyQuote(groupId)}/members/${pyQuote(memberAic)}`,
    );
  }

  async removeMember({ leaderAic, groupId, memberAic }: GroupMemberRef): Promise<void> {
    await this._request(
      "DELETE",
      `/groups/${pyQuote(leaderAic)}/${pyQuote(groupId)}/members/${pyQuote(memberAic)}`,
    );
  }

  async deleteGroup({ leaderAic, groupId }: Omit<GroupMemberRef, "memberAic">): Promise<void> {
    await this._request("DELETE", `/groups/${pyQuote(leaderAic)}/${pyQuote(groupId)}`);
  }

  async closeMemberConnection({ leaderAic, groupId, memberAic }: GroupMemberRef): Promise<void> {
    await this._request(
      "DELETE",
      `/groups/${pyQuote(leaderAic)}/${pyQuote(groupId)}/members/${pyQuote(memberAic)}/connection`,
    );
  }

  /** 对应 Python `aclose()`；Node 传输层每次请求独立连接，无需释放。 */
  async aclose(): Promise<void> {
    return undefined;
  }

  /** @internal */
  async _request(method: string, path: string): Promise<void> {
    logger.info(
      `event=group_acl_client_request method=${method} base_url=${redactUrl(this.baseUrl)} path=${path}`,
    );
    // httpx 的 base_url 合并规则：base_url 补尾斜杠后拼接去掉前导斜杠的 path
    const url = `${this.baseUrl}/${path.replace(/^\/+/, "")}`;
    let response: HttpResponseData;
    try {
      response = await this.transport({ url, method, timeoutMs: this.timeoutMs });
    } catch (error) {
      // 与 Python 不同（Python 让 httpx 的传输层异常原样抛出）：连接失败 / 超时同样包装成
      // GroupAclError，调用方的 `except GroupAclError` 式回滚与超时清理才能覆盖这类故障。
      const detail = error instanceof Error ? error.message : String(error);
      logger.error(
        `event=group_acl_client_request_failed method=${method} path=${path} status=None detail=${detail}`,
      );
      throw new GroupAclError(`${method} ${path} failed: ${detail}`, { cause: error });
    }
    if (response.status >= 400) {
      const detail =
        pyStrip(response.body.toString("utf-8")) || (STATUS_CODES[response.status] ?? "");
      logger.error(
        `event=group_acl_client_request_failed method=${method} path=${path} status=${response.status} detail=${detail}`,
      );
      throw new GroupAclError(`${method} ${path} failed: ${response.status} ${detail}`);
    }
    logger.info(
      `event=group_acl_client_request_done method=${method} path=${path} status=${response.status}`,
    );
  }
}

/** GroupLeader 依赖的 ACL 客户端接口（便于测试替换）。 */
export type GroupAclClientLike = Pick<
  GroupAclClient,
  "addMember" | "removeMember" | "deleteGroup" | "closeMemberConnection" | "aclose"
>;
