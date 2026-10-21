/**
 * AIP v2 SDK 公共导出（对应 Python `acps_sdk.aip`）
 */

// AIP v2 基础类型
export {
  TaskState,
  TaskStateSchema,
  TaskCommandType,
  TaskCommandTypeSchema,
  TERMINAL_TASK_STATES,
  TextDataItemSchema,
  FileDataItemSchema,
  StructuredDataItemSchema,
  DataItemSchema,
  TaskStatusSchema,
  ProductSchema,
  GetCommandParamsSchema,
  StartCommandParamsSchema,
  SenderRoleSchema,
  MentionsSchema,
  MessageSchema,
  TaskCommandSchema,
  TaskResultSchema,
  isTextDataItem,
  isFileDataItem,
  isStructuredDataItem,
} from "./baseModel.js";
export type {
  TextDataItem,
  FileDataItem,
  StructuredDataItem,
  DataItem,
  TaskStatus,
  Product,
  GetCommandParams,
  StartCommandParams,
  SenderRole,
  Message,
  TaskCommand,
  TaskCommandInput,
  TaskResult,
  TaskResultInput,
} from "./baseModel.js";

// JSON-RPC 模型
export {
  JsonRpcIdSchema,
  JSONRPCRequestSchema,
  JSONRPCErrorSchema,
  JSONRPCResponseSchema,
  RpcRequestParamsSchema,
  RpcRequestSchema,
  RpcResponseSchema,
} from "./rpcModel.js";
export type {
  JsonRpcId,
  JSONRPCRequest,
  JSONRPCError,
  JSONRPCResponse,
  RpcRequestParams,
  RpcRequest,
  RpcResponse,
} from "./rpcModel.js";

// 身份绑定
export {
  AUTHENTICATION_REQUIRED_CODE,
  AUTHORIZATION_FAILED_CODE,
  AipIdentityError,
  InvalidPeerCertificateError,
  PeerAicMissingError,
  SenderIdentityMismatchError,
  assertAicMatchesExpected,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  extractAcpsUriSan,
  extractCommonName,
  extractPeerAicFromResponse,
  extractPeerAicFromSocket,
  extractPeerIdentity,
  identityErrorToJsonRpc,
  normalizeAic,
  normalizeAndValidateAic,
  normalizeAndValidateExpectedAic,
  parseSubjectAltName,
} from "./identity.js";
export type { AipIdentityBindingConfig, PeerCertificateLike, PeerIdentity } from "./identity.js";

export {
  PeerCertificateInfo,
  PeerCertificateRegistry,
  buildPeerCertificateInfo,
  getRequestPeerAic,
  getRequestPeerCertificateInfo,
  registry as peerCertificateRegistry,
  setRequestPeerIdentity,
} from "./peerCert.js";

// RPC 服务端
export {
  DefaultHandlers,
  TaskManager,
  createAipRpcRequestListener,
  handleRpcRequest,
} from "./rpcServer.js";
export type {
  AipRpcListenerOptions,
  CommandHandlers,
  HandleRpcRequestOptions,
  RpcRequestContext,
  StartHandler,
  TaskHandler,
} from "./rpcServer.js";

// HTTP 传输层（客户端抛出的 HttpStatusError、可注入的 transport 类型）
export {
  HttpStatusError,
  createNodeStreamTransport,
  createNodeTransport,
} from "../internal/http.js";
export type {
  HttpRequestOptions,
  HttpResponseData,
  HttpStreamResponse,
  HttpStreamTransport,
  HttpTransport,
  TlsClientOptions,
} from "../internal/http.js";

// RPC 客户端
export { AipRpcClient } from "./rpcClient.js";
export type { AipRpcClientOptions } from "./rpcClient.js";
export {
  errorInfoFromRpcResponse,
  statusFromRpcError,
  statusFromRpcResponse,
} from "./accessStatus.js";

// -----------------------------------------------------------------------
// 流式传输（Streaming）
// -----------------------------------------------------------------------

export {
  SSE_HEADERS,
  SSE_MEDIA_TYPE,
  StreamEventDataSchema,
  StreamEventPayloadSchema,
  StreamRequestSchema,
  StreamRequestParamsSchema,
  StreamResponseSchema,
  TaskStatusUpdateEventSchema,
  ProductChunkEventSchema,
  ReStreamCommandParamsSchema,
} from "./streamModel.js";
export type {
  StreamEventData,
  StreamEventPayload,
  StreamRequest,
  StreamRequestParams,
  StreamResponse,
  TaskStatusUpdateEvent,
  ProductChunkEvent,
  ReStreamCommandParams,
} from "./streamModel.js";

export {
  StreamHub,
  TaskStreamChannel,
  buildStreamErrorResponse,
  buildStreamResponse,
  createAipStreamRequestListener,
  formatSse,
  handleStreamRequest,
  writeStreamOutcome,
} from "./streamServer.js";
export type {
  AipStreamListenerOptions,
  BufferedStreamEvent,
  HandleStreamRequestOptions,
  StreamHandlers,
  StreamPayload,
  StreamRequestOutcome,
} from "./streamServer.js";

export {
  AipStreamClient,
  StreamProtocolError,
  isReconnectableStreamError,
} from "./streamClient.js";
export type { AipStreamClientOptions } from "./streamClient.js";

// -----------------------------------------------------------------------
// 通知方式（Notification）
// -----------------------------------------------------------------------

export {
  NOTIFICATION_TOKEN_HEADER,
  NotificationConfigSchema,
  NotificationDeleteRequestSchema,
  NotificationDeleteResponseSchema,
  NotificationDeleteResultSchema,
  NotificationGetRequestSchema,
  NotificationGetResponseSchema,
  NotificationIdParamsSchema,
  NotificationRequestSchema,
  NotificationResponseSchema,
  NotificationStartParamsSchema,
  NotificationStartRequestSchema,
  NotificationStartRequestParamsSchema,
} from "./notificationModel.js";
export type {
  NotificationConfig,
  NotificationDeleteRequest,
  NotificationDeleteResponse,
  NotificationDeleteResult,
  NotificationGetRequest,
  NotificationGetResponse,
  NotificationIdParams,
  NotificationRequest,
  NotificationResponse,
  NotificationStartParams,
  NotificationStartRequest,
  NotificationStartRequestParams,
} from "./notificationModel.js";

export {
  NotificationConfigNotFoundError,
  NotificationConfigStore,
  NotificationDispatcher,
  NotificationParamsError,
  NotificationRegistry,
  NotificationService,
  createAipNotificationRequestListener,
  handleNotificationRequest,
  resolveNotificationOwner,
} from "./notificationServer.js";
export type {
  NotificationDispatcherOptions,
  NotificationHandlers,
  NotificationMethod,
  NotificationServiceOptions,
  NotificationSubscription,
} from "./notificationServer.js";

export {
  AipNotificationClient,
  NotificationReceiver,
  constantTimeEquals,
  createAipNotificationReceiverListener,
} from "./notificationClient.js";
export type {
  AipNotificationClientOptions,
  NotificationReceiverOptions,
} from "./notificationClient.js";

// -----------------------------------------------------------------------
// 群组模式（Group Mode）— 由 groupModel / groupIdentity / groupLeader / groupPartner 提供
// -----------------------------------------------------------------------

export {
  ACSObjectSchema,
  AMQPConfigSchema,
  GroupInfoSchema,
  GroupInvitationErrorDataSchema,
  GroupInvitationErrorSchema,
  GroupMemberStatusSchema,
  GroupMgmtCommand,
  GroupMgmtCommandSchema,
  GroupMgmtCommandType,
  GroupMgmtCommandTypeSchema,
  GroupMgmtResultSchema,
  InboxGroupInvitationErrorSchema,
  InboxGroupInvitationSchema,
  RabbitMQRequestParamsSchema,
  RabbitMQRequestSchema,
  RabbitMQResponseErrorDataSchema,
  RabbitMQResponseErrorSchema,
  RabbitMQResponseResultSchema,
  RabbitMQResponseSchema,
  RabbitMQServerConfigSchema,
} from "./groupModel.js";
export type {
  ACSObject,
  AMQPConfig,
  GroupInfo,
  GroupInvitationError,
  GroupInvitationErrorData,
  GroupInvitationErrorInput,
  GroupMemberStatus,
  GroupMgmtCommandInput,
  GroupMgmtResult,
  GroupMgmtResultInput,
  InboxGroupInvitation,
  InboxGroupInvitationError,
  InboxGroupInvitationErrorInput,
  InboxGroupInvitationInput,
  RabbitMQRequest,
  RabbitMQRequestInput,
  RabbitMQRequestParams,
  RabbitMQResponse,
  RabbitMQResponseError,
  RabbitMQResponseErrorData,
  RabbitMQResponseInput,
  RabbitMQResponseResult,
  RabbitMQServerConfig,
} from "./groupModel.js";

export {
  assertDirectGroupRequestIdentity,
  assertIncomingGroupMessageIdentity,
  buildOutgoingAmqpUserId,
  extractAmqpUserId,
} from "./groupIdentity.js";
export type { AmqpUserIdCarrier } from "./groupIdentity.js";

export { GroupAclClient, GroupAclError } from "./groupAuth.js";
export type { GroupAclClientLike, GroupAclClientOptions, GroupMemberRef } from "./groupAuth.js";

export {
  DEFAULT_INVITATION_TIMEOUT_SECONDS,
  INBOX_EXCHANGE_NAME,
  INBOX_MESSAGE_TTL_MS,
  INBOX_QUEUE_EXPIRES_MS,
  INVITATION_CLOCK_SKEW_SECONDS,
  AsyncEvent,
  buildExternalConnectionUrl,
  buildGroupExchangeName,
  buildGroupQueueName,
  buildInboxQueueName,
  buildPlainConnectionUrl,
  calculateInvitationExpiry,
  createInvitationToken,
  ensureValidAic,
  ensureValidGroupId,
  getEndpointUrl,
  invitationIsExpired,
  normalizeGroupId,
  parseAmqpEndpointUrl,
  parseUtcDatetime,
  replaceAicPlaceholder,
} from "./groupRuntime.js";
export type { AMQPEndpoint } from "./groupRuntime.js";

export {
  ChannelClosedError,
  ChannelInvalidStateError,
  DeliveryError,
  DeliveryMode,
  ExchangeType,
  MessageProcessError,
  AmqpMessage,
  createAmqplibConnector,
  defaultAmqpConnector,
} from "./groupAmqp.js";
export type {
  AmqpChannel,
  AmqpConnectOptions,
  AmqpConnection,
  AmqpConnector,
  AmqpConsumeCallback,
  AmqpExchange,
  AmqpIncomingMessage,
  AmqpMessageOptions,
  AmqpQueue,
  AmqplibConnectorOptions,
  DeclareExchangeOptions,
  DeclareQueueOptions,
} from "./groupAmqp.js";

export {
  GroupLeader,
  GroupLeaderMqClient,
  GroupLeaderSession,
  GroupLeaderState,
  PartnerConnectionInfo,
  PartnerInviteError,
  PartnerNetworkError,
  PartnerResponseError,
} from "./groupLeader.js";
export type {
  ClientCertificate,
  ForceRemovePartnerResult,
  GroupLeaderMqClientOptions,
  GroupLeaderOptions,
  GroupLeaderRabbitMQConfig,
  GroupMessage,
  GroupRuntimeInfo,
  GroupRuntimeMember,
  InvitePartnerOptions,
  MessageEmitterLike,
  MessageHandler,
  PartnerTaskSnapshot,
  PendingInvitation,
  TaskSummary,
  TraceContextProvider,
} from "./groupLeader.js";

export {
  GroupPartnerMqClient,
  PartnerGroupSession,
  PartnerGroupState,
  extractTextFromCommand,
} from "./groupPartner.js";
export type {
  DisconnectHandler,
  GroupPartnerMqClientOptions,
  InvitationHandler,
  MgmtCommandHandler,
  PartnerTaskRecord,
  TaskCommandHandler,
  TaskResultHandler,
} from "./groupPartner.js";
