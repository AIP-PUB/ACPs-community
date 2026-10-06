[Home](../README.md)

**[English](aip-identity-binding-verification_en.md) | [中文](aip-identity-binding-verification.md)**

# How AIP Communication Prevents Identity Forgery

This tutorial answers a common question: in AIP communication, how do you prevent one Agent from impersonating another Agent when sending business messages?

First, the conclusion. AIP prevents identity forgery by relying on two techniques used together:

1. mTLS proves "who the peer on this connection is".
2. Comparing the certificate identity against the business data proves "the sender claimed by the business message really is that peer on the connection".

Doing only the first step is not enough. mTLS can confirm that the peer holds a valid certificate, but the `senderId` in a business message is part of the JSON body, and the client can construct it however it likes. If a legitimate Agent establishes a connection with its own certificate yet writes someone else's `senderId` in the body, that is business identity forgery.

Therefore AIP needs to bind the connection identity to the business identity:

```text
connection identity == senderId of the business message
```

Related specifications:

- [ACPs AIP (Agent Interaction Protocol) Specification](../../acps-specs/07-ACPs-spec-AIP/ACPs-spec-AIP_en.md)
- [AIA Identity Authentication Specification](../../acps-specs/05-ACPs-spec-AIA/ACPs-spec-AIA_en.md)

---

## 1. What problem does mTLS solve

mTLS solves the problem of "identity authentication at the connection layer".

In direct AIP communication, both sides use HTTPS + mTLS. The server verifies the client certificate, and the client can also verify the server certificate. The CN in the certificate is the Agent's AIC, and the SAN can serve as supplementary identity information.

This means the receiver can know:

- This TLS connection comes from an Agent holding a valid certificate.
- The CN in the certificate can serve as the AIC of this connection's peer.
- The connection contents are protected by TLS and are not easily eavesdropped on or tampered with in transit.

But mTLS does not automatically understand the AIP body. It will not check for you whether the `senderId` in the JSON is honest.

For example:

```text
TLS certificate CN: 1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
body.senderId: 1.2.156.3088.1.1.34C2.478BDF.3GF548.0P65
```

From the mTLS perspective, this connection is legitimate, because the certificate is genuine.  
From the AIP business perspective, this message should be rejected, because the sender claimed by the business message does not match the certificate identity.

---

## 2. What problem does comparing certificate and business data solve

Comparing the certificate and the business data solves the problem of "business identity forgery".

The `senderId` in an AIP message indicates "who this business message claims to come from". But `senderId` lives in the business data, so the sender can fill it in freely. The receiver cannot trust that a message came from a certain AIC merely because that AIC is written in the body.

Therefore the receiver needs to perform a check like this:

```text
obtain peer AIC from the mTLS certificate
read AIP body.senderId
compare peer AIC == body.senderId
```

If they are equal, the connection identity and the business identity match, and processing can continue.  
If they are not equal, the sender claimed another identity in the business data, and the message should be rejected.

This is Identity Binding. It does not replace mTLS; rather, after mTLS has already proven the connection identity, it further constrains that identity onto the AIP business message.

---

## 3. How Direct, Stream, and Notification prevent forgery

Direct RPC, Stream, and Notification callback are all direct HTTP communication, so the rule is the same.

```text
mTLS peer certificate CN
  -> peer AIC
  -> must equal the senderId of the AIP payload
```

You can think of it as the following checklist:

| Source | Meaning | Trustworthy |
| --- | --- | --- |
| mTLS certificate CN | AIC of the connection peer | Trustworthy, comes from certificate verification |
| body.senderId | Sender declared by the business message | Must be verified, cannot be trusted on its own |

All the receiver has to do is compare the two:

```text
AIC in certificate CN == body.senderId
```

If they are not equal, the message should be rejected before it reaches the business handler.

Notification callback follows the same logic. The callback request body usually contains a `TaskResult`, and its `senderId` must equal the mTLS peer AIC of the callback request.

---

## 4. How Group / RabbitMQ prevents forgery

Group mode is different. Messages are not sent over a direct HTTP connection between the receiver and the sender; instead they go through RabbitMQ.

Therefore the consumer cannot directly compare "the CN of the current connection's mTLS certificate" with `body.senderId`, because what the consumer sees is the RabbitMQ connection, not the original sender's connection.

Group mode needs to carry the identity in two segments:

```text
publisher mTLS certificate CN
  -> RabbitMQ authenticated username
  -> AMQP user_id
  -> message body.senderId
```

There are two layers of verification here:

1. RabbitMQ verifies `AMQP user_id == authenticated username`.
2. The consumer SDK verifies `body.senderId == AMQP user_id`.

The reasons for doing this are:

- RabbitMQ can verify who the publishing connection is, but it cannot see and should not interpret the AIP JSON body.
- The consumer SDK can parse the AIP body, but it has no access to the publisher's TLS connection at that time.
- `AMQP user_id` is the bridge that carries identity between the two.

If a publisher tries to forge `user_id`, RabbitMQ should reject the publish.  
If a publisher uses a real `user_id` but forges `senderId` in the body, the consumer SDK should reject the consumption.

---

## 5. Why active forgery verification is recommended

Reading code and inspecting configuration can only show that "the system appears to have verification". A more reliable approach is to actively construct a fake message and see whether it really cannot get past the boundary.

The key to active forgery verification is not forging certificates. Certificates cannot be forged in a normal system. What we need to do is:

1. Establish a connection using a real, legitimate certificate.
2. Deliberately modify a field the attacker can control, such as `body.senderId` or the AMQP `user_id`.
3. Observe whether the system rejects it.

This verifies three things:

- Whether the mTLS identity really reaches the SDK or RabbitMQ.
- Whether the SDK or RabbitMQ really performs the identity comparison.
- Whether the forged message is rejected before the business logic produces side effects.

A good forgery test should observe all of the following at once:

- Whether the returned status or error code matches expectations.
- Whether the error message indicates an identity mismatch.
- Whether the business handler did not execute.
- Whether business state such as queues, tasks, and group queues was not incorrectly created or updated.

Each scenario below first explains the verification logic and then gives the e2e sample already written in the project. The commands assume they are executed from the ACPs workspace root, that is, the directory containing `demo-leader`, `demo-partner`, `mq-auth-server`, and `acps-docs`.

---

## 6. Verifying Direct: forging `senderId`

The verification method for Direct RPC is:

1. Connect to Partner using the real Leader certificate.
2. Construct an AIP RPC request.
3. Change the `senderId` in the request body to an AIC that is not the Leader's.
4. Send the request.
5. Observe whether Partner rejects it.

What needs to be verified is not "whether the request can be sent out", but "whether, after the request reaches the Partner boundary, it is rejected because of the identity mismatch".

Forgery sample:

```text
certificate identity: 1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
body.senderId: 1.2.156.3088.1.1.34C2.478BDF.3GF548.0P65
expected result: reject
```

You should observe:

- The RPC returns a JSON-RPC error.
- The error code is `-32009`.
- The error data or error message contains `senderId`.
- Partner's business processing logic did not execute.

The project already has a corresponding e2e sample, which you can run directly:

```bash
cd demo-partner
uv run pytest tests/e2e/test_identity_binding.py::test_direct_rpc_rejects_forged_sender_id -q
```

The verification logic for Stream is the same, except that the response form is HTTP/SSE:

```bash
cd demo-partner
uv run pytest tests/e2e/test_identity_binding.py::test_stream_rejects_forged_sender_id -q
```

You should observe:

- The HTTP status is `403`.
- The response detail contains `-32009`.
- The error message mentions `senderId`.
- No normal business streaming events are produced.

---

## 7. Verifying Notification callback: forging `TaskResult.senderId`

The verification method for Notification callback is:

1. Call Leader's callback endpoint using the real Partner certificate.
2. Construct a `TaskResult`.
3. Change `TaskResult.senderId` to an AIC that is not the Partner's, for example to the Leader's AIC.
4. Send the callback request.
5. Observe whether Leader rejects it.

Forgery sample:

```text
certificate identity: 1.2.156.3088.1.1.34C2.478BDF.3GF547.0GGS
TaskResult.senderId: 1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
expected result: reject
```

You should observe:

- The HTTP status is `403`.
- The response detail contains `-32009`.
- The error message mentions `senderId`.
- Leader should not treat this forged callback as a genuine Partner result.

The project already has a corresponding e2e sample:

```bash
cd demo-leader
uv run pytest tests/e2e/test_notification_identity.py::test_notification_receiver_rejects_forged_sender_id_with_valid_partner_cert -q
```

---

## 8. Verifying Group: forging the AMQP `user_id`

The first layer of Group mode is to verify whether RabbitMQ rejects a forged `user_id`.

The verification method is:

1. Connect to RabbitMQ using some real Agent certificate.
2. When publishing a message, set the AMQP `user_id` to another AIC.
3. Observe whether RabbitMQ rejects the publish.

Forgery sample:

```text
mTLS authenticated username: 1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
AMQP user_id: 1.2.156.3088.1.1.34C2.478BDF.3GF548.0P65
expected result: RabbitMQ rejects the publish
```

You should observe:

- The message publish fails.
- RabbitMQ or the client returns a permission-related error.
- The mq-auth-server logs show the username and the authorization result.
- The message does not enter the target queue.

The project already has a corresponding e2e sample:

```bash
cd mq-auth-server
uv run pytest tests/e2e/test_rabbitmq_user_id_identity.py::test_validated_user_id_rejects_forged_user_id -q
```

If this verification does not fail, first check whether RabbitMQ has EXTERNAL/mTLS enabled, and whether ordinary Agents have been incorrectly granted permission to forge `user_id`.

---

## 9. Verifying Group: forging body.senderId

The second layer of Group mode is to verify whether the consumer SDK rejects a forged identity in the body.

The point of this step is: `AMQP user_id` is real, but `body.senderId` is fake.

The verification method is:

1. Publish a message using a real Agent identity.
2. Make the AMQP `user_id` equal to the publisher's own AIC.
3. Change the `senderId` in the message body to another AIC.
4. Let the consumer receive the message.
5. Observe whether the consumer SDK rejects it, and confirm that the business state was not incorrectly changed.

Forgery sample:

```text
AMQP user_id: 1.2.156.3088.1.1.34C2.478BDF.3GF547.0GGS
body.senderId: 1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
expected result: consumer SDK rejects
```

You should observe:

- The consumer logs an identity mismatch error.
- The business handler should not process this forged message.
- A forged group queue should not be created.
- No subsequent erroneous business messages should be produced.

The project already has a corresponding e2e sample:

```bash
cd demo-leader
uv run pytest tests/e2e/test_group_identity.py::test_partner_inbox_consumer_rejects_forged_sender_identity -q
```

This verification matters, because RabbitMQ cannot read the JSON body. Even if RabbitMQ has already verified the `user_id`, the consumer SDK still needs to check `body.senderId == AMQP user_id`.

---

## 10. Which logs to look at during troubleshooting

Active forgery testing is the most reliable verification method; logs are mainly used for troubleshooting.

You can look at these places:

- `mq-auth-server` logs: search for `rabbitmq_auth_decision`, `authz_decision`, `username`, `decision`.
- demo-leader / demo-partner application logs: search for `senderId`, `identity binding`, `AuthorizationFailedError`, `-32009`.
- AMP logs: `demo-leader/logs/amp_*.jsonl`, `demo-partner/logs/amp_*.jsonl`.

Note: the current AMP audit / access / message logs are better suited to viewing tasks, access, and message flow; they are not dedicated identity-binding verdict logs. They can assist troubleshooting, but they cannot replace active forgery testing.

---

## 11. Minimal verification checklist

If you only want to quickly confirm whether AIP identity anti-forgery works, you can run the following forgery tests:

```bash
cd demo-partner
uv run pytest tests/e2e/test_identity_binding.py::test_direct_rpc_rejects_forged_sender_id -q
uv run pytest tests/e2e/test_identity_binding.py::test_stream_rejects_forged_sender_id -q

cd ../demo-leader
uv run pytest tests/e2e/test_notification_identity.py::test_notification_receiver_rejects_forged_sender_id_with_valid_partner_cert -q
uv run pytest tests/e2e/test_group_identity.py::test_partner_inbox_consumer_rejects_forged_sender_identity -q

cd ../mq-auth-server
uv run pytest tests/e2e/test_rabbitmq_user_id_identity.py::test_validated_user_id_rejects_forged_user_id -q
```

If all these tests pass, it basically shows that:

- Direct RPC / Stream can reject a forged `senderId`.
- Notification callback can reject a forged `TaskResult.senderId`.
- RabbitMQ can reject a forged `user_id`.
- The Group consumer can reject `body.senderId != AMQP user_id`.

This is the core closed loop by which AIP communication prevents identity forgery.
