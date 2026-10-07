[Home](../README_en.md)

**[English](ACPs-spec-overview_en.md) | [中文](ACPs-spec-overview.md)**

ACPs: Agent Collaboration Protocols for Agent Interconnection (ACPs-spec-v02.02)

# 1. Document Definition

This document is the overall introduction to the ACPs agent collaboration protocol suite, version v02.02.

The full title of the document is ACPs-spec-v02.02.

Document authors: Jun Liu (Beijing University of Posts and Telecommunications), Ke Li (Beijing University of Posts and Telecommunications), Keliang Chen (Beijing University of Posts and Telecommunications), Ke Yu (Beijing University of Posts and Telecommunications), Xiaofeng Hu (Beijing University of Posts and Telecommunications), Di Ma (Beijing University of Posts and Telecommunications), Ge Gao (China Electronics Standardization Institute).

# 2. Overview of the Agent Collaboration Protocols

Agent interconnection refers to the process in which agents connect with other agents through protocols or interfaces to achieve cross-platform and cross-domain interconnection and collaboration on complex tasks. The purpose of agent interconnection is to break through the capability limits of a single agent while removing the constraints of vendors' proprietary multi-agent frameworks, and to build, in an open and interconnected form, a platform on which agents can communicate as equals, interconnect and collaborate, and benefit one another. Agents can then self-organize and self-negotiate into an efficient collaboration network, dynamically adjusting resource allocation and collaboration patterns according to user task requirements, thereby improving the overall capability and efficiency of the agent system.

As the core foundation of agent interconnection, protocols related to agent interconnection have recently become a focus of attention in both academia and industry. After Anthropic proposed the Model Context Protocol (MCP) for connecting large models with tools, Google proposed the Agent2Agent (A2A) protocol for communication between agents. In addition, the independent Chinese researcher Chang Gaowei released the Agent Network Protocol (ANP) for agent networks in June 2024. More research work and results are introduced fairly comprehensively in the recent Arxiv paper "A Survey of AI Agent Protocols". Although the emergence of these protocols for agent interconnection scenarios has greatly advanced the development of agent interconnection, each of them was initially designed with only one specific scenario in mind. For example, MCP focuses on how large models invoke tools, A2A aims to solve the problem of agent interconnection between enterprises, and ANP is relatively comprehensive but, in order to preserve a free space for interconnection among agents, does not devote much design to the manageability of agents.

Starting from the vision that agent interconnection will become critical network infrastructure in the future, and building on the research results above, this document attempts to propose and design, from a more holistic perspective, a suite of agent collaboration protocols for agent interconnection: the Agent Collaboration Protocols (ACPs). This protocol suite covers multiple functional areas such as agent registration, agent discovery, and agent interaction, so as to fill the gaps in existing research and provide new ideas and methods for the solid development of agent interconnection. It should be particularly noted that this document constructs the ACPs protocol suite for the first time and may still have certain incompleteness and shortcomings. We welcome readers' corrections and discussion so that we can improve the protocol suite together.

# 3. Entities in the Agent Collaboration Protocols and Their Relationships

The entities in the Agent Collaboration Protocols (ACPs) and their relationships are shown in the figure below.

![1-1_en.png](1-1_en.png)

The entities in the Agent Collaboration Protocols include the following categories:

● Agent Registration Service Provider (ARSP): An agent that is secure and controllable should register its capabilities with an agent registration service provider and obtain an identity identifier. In agent interconnection, given the huge number of agents and their wide geographical distribution, there may be multiple agent registration service providers, each responsible for managing a certain number of agent registrations.

● Certificate Authority Service Provider (CASP): An agent that is secure and controllable should obtain a digital identity certificate corresponding to its identity identifier from a trusted certificate authority service provider. In agent interconnection, given the huge number of agents and their wide geographical distribution, there may be multiple certificate authority service providers, each responsible for managing a certain number of agent identity certificates. To ensure the reliability of agent identity certificate issuance, certificate authority service providers and agent registration service providers need to synchronize information.

● Agent Discovery Service Provider (ADSP): Through standardized processes and protocols, agent discovery service providers enable agents to identify and understand the capabilities of one or several agents. Given the huge number of agents and their wide geographical distribution, agent discovery query requests are numerous and widespread; therefore, in agent interconnection there may be multiple agent discovery service providers so that agents or users can discover and make use of the capabilities of each agent. To ensure the accuracy and timeliness of capability information, agent discovery service providers and agent registration service providers need to synchronize information.

● Agents: Agents are the core execution units in agent interconnection. Each agent is created by a specific agent provider and must, before providing services, first register with an agent registration service provider to obtain an identity identifier, and then obtain an identity certificate from a certificate authority service provider. After receiving a task assigned by a user, when other agents are needed to complete the task collaboratively, the agent can look for suitable collaborating agents through agent discovery service providers in order to achieve the task goal.

● Message Queue: A message queue is a message management component that exists to support the complex and dynamic interaction requirements among agents. Message queues are built by specific message queue service providers that offer capability services. Because the number of agents in agent interconnection will grow rapidly and the volume of interaction messages will therefore be enormous, there will be a fairly large number of message queue service providers offering message queue services.

# 4. Specifications Defined by the Agent Collaboration Protocols

The Agent Collaboration Protocols (ACPs) is a standardized communication and interaction protocol suite designed to guarantee efficient collaboration among heterogeneous agents and to support diverse agent interconnection applications. ACPs defines the following specifications and protocols:

[(1) Agent Identity Code (AIC) Specification](../02-ACPs-spec-AIC/ACPs-spec-AIC_en.md)

[(2) Agent Capability Specification (ACS) Specification](../03-ACPs-spec-ACS/ACPs-spec-ACS_en.md)

[(3) Agent Trusted Registration (ATR) Specification](../04-ACPs-spec-ATR/ACPs-spec-ATR_en.md)

[(4) Agent Identity Authentication (AIA) Specification](../05-ACPs-spec-AIA/ACPs-spec-AIA_en.md)

[(5) Agent Discovery Protocol (ADP) Specification](../06-ACPs-spec-ADP/ACPs-spec-ADP_en.md)

[(6) Agent Interaction Protocol (AIP) Specification](../07-ACPs-spec-AIP/ACPs-spec-AIP_en.md)

[(7) Data Synchronization Protocol (DSP) Specification](../08-ACPs-spec-DSP/ACPs-spec-DSP_en.md)

[(8) Agent Monitoring Protocol (AMP) Specification](../09-ACPs-spec-AMP/ACPs-spec-AMP_en.md)

# 5. Main Content of Each Specification

## 5.1. Core Concepts

- **User**: The actual user of agent interconnection, who connects to agent interconnection by interacting with a personal assistant.
- **Personal assistant agent**: A type of agent that directly receives user requests and is responsible for decomposing user requirements into subtasks and finding suitable agents to complete those subtasks.
- **Agent Identity Code (AIC)**: The authenticatable identity identifier of each agent, which comes from the agent registration service provider with which the agent first registers.
- **Agent Capability Specification (ACS)**: A concise yet reasonably flexible way of expressing agent capabilities.
- **Certificate of Agent Identity (CAI)**: Because agents must use the HTTPS protocol for data transmission, a CAI is required.
- **Agent Interaction Protocol (AIP)**: In multi-agent systems, context management is of the utmost importance. In AIP we define multiple interaction modes to satisfy different interaction requirements.

## 5.2. Definition of the Agent Identity Code

For agent interconnection to become a secure and reliable agent system, the primary condition is that the agents running within it with the ability to execute tasks autonomously should be secure and reliable instances. To meet this condition, each agent should have an authenticatable identity identifier, which we define as the Agent Identity Code (AIC).

Each agent should have a unique AIC, which comes from the agent registration service provider (ARSP) with which the agent first registers. In agent interconnection, there may be multiple agent registration service providers, and each provider should be a service entity that has been recognized by consensus (for example, certified by a management authority).

The Agent Identity Code consists of multiple levels of identifiers in sequence, separated by the `.` symbol. Each identifier uses Arabic numerals or English letters, where Arabic numerals range from 0 to 9 and English letters range from A to Z (case-insensitive; uppercase is recommended). The following is an example AIC:

```
1.2.156.3088.1.34C2.478BDF.3GF546.1.0SEN
```

Here 1.2.156.3088 is the Agent Identity Code prefix, and 1.34C2.478BDF.3GF546.1.0SEN is the Agent Identity Code content. Explanation:

**Identity code prefix**
- (1) Top-level arc: 1 indicates ISO.
- (2) Second-level arc: 2 indicates a national member body.
- (3) Level 3: 156 indicates China.
- (4) Level 4: The dedicated node for agent interconnection approved by the national OID registration authority.

**Identity code content**
- (5) Level 5: Agent registration service provider index (value range 1–ZZZZZZ). 1 indicates one agent registration service provider A.
- (6) Level 6: Agent provider index (value range 1–ZZZZZZ). 34C2 indicates the identifier assigned by agent registration service provider A to agent provider B.
- (7) Level 7: Agent ontology serial number (value range 1–ZZZZZZZZZ). 478BDF indicates an agent ontology serial number identifier assigned by agent registration service provider A to agent provider B.
- (8) Level 8: Agent entity serial number (value range 0–ZZZZZZZZZ). 3GF546 indicates the agent entity serial number identifier created from the agent ontology with serial number 478BDF. When the registration object is an agent ontology, its agent entity serial number is identified by the special character 0.
- (9) Level 9: Agent Identity Code version number (value range 1–Z). 1 indicates that the Agent Identity Code version is 1.
- (10) Level 10: Checksum (value range 0000–1EKF). In the example the checksum is 0SEN. For how the checksum is generated and verified, please refer to [ACPs-spec-AIC](../02-ACPs-spec-AIC/ACPs-spec-AIC_en.md).

## 5.3. Agent Capability Specification

For agent interconnection to become a secure and reliable agent system, one important capability it needs is to support agents in describing their own capabilities, storing those descriptions, and retrieving them. The way agent capabilities are described must be standardized enough to facilitate interconnection among agents, and also flexible enough to accommodate the complex capability expressions of large-model-based agents. To achieve these goals, we define the Agent Capability Specification (ACS).

For the definitions of objects such as AgentCapabilitySpec, AgentProvider, and AgentCapabilities, please refer to [ACPs-spec-ACS](../03-ACPs-spec-ACS/ACPs-spec-ACS_en.md).

## 5.4. Agent Trusted Registration

For agent interconnection to become a secure and reliable agent system, the agents running within it with the ability to execute tasks autonomously should be secure and reliable entities. To achieve this goal, each agent should satisfy the following two necessary conditions:

(1) Obtain a globally unique identity identifier from an agent registration service provider; this identifier is the Agent Identity Code (AIC, for details see [ACPs-spec-AIC](../02-ACPs-spec-AIC/ACPs-spec-AIC_en.md));

(2) Obtain a digital certificate usable for identity verification, called the Certificate of Agent Identity (CAI), from the certificate authority service provider designated by the agent registration service provider.

An agent registration process that satisfies the above two conditions can be called an agent trusted registration process. It is mainly divided into two processes: the agent ontology registration process and the agent entity trusted registration process.

### 5.4.1 Agent Ontology Trusted Registration Process

The agent provider sends a registration request containing ACS information (including ca-challenge-url) to the agent registration service provider, which assigns an AIC after manual review and approval. If the provider applies for a certificate from the certificate authority service provider (CA Server) for the first time, it must first register an account and then apply for the agent identity certificate through the CA Client tool. The CA Server obtains and checks the agent's detailed information from the agent registration service provider based on the AIC, and then initiates identity challenge verification. The agent provider deploys the verification information on the specified Challenge Server, and after the CA Server verifies it successfully, the agent identity certificate is generated and issued.

### 5.4.2 Agent Entity Trusted Registration Process

The agent provider uses the already obtained agent ontology certificate to establish a mutual authentication channel (such as mTLS) with the agent registration service provider, and submits an entity registration application containing the ontology AIC and additional information. Based on the AIC in the ontology certificate, the agent registration service provider retrieves and automatically verifies the corresponding ACS; after verification succeeds, it generates an entity serial number, combines it with the ontology AIC to generate the entity AIC, saves the entity ACS, and returns the entity AIC to the agent provider. After obtaining the entity AIC, the agent provider completes the application for and acquisition of the agent entity identity certificate by following the ontology certificate application process.

## 5.5. Agent Identity Authentication

For agent interconnection to become secure and reliable network infrastructure, the agents running within it with the ability to execute tasks autonomously should be secure and reliable entities. In the agent trusted registration process standard of the ACPs protocol suite, two necessary conditions are provided for each agent: the Agent Identity Code (AIC) and the Certificate of Agent Identity (CAI).

Agent identity authentication can use a variety of authentication protocols. It is recommended that agents use mTLS with the TLS 1.3 protocol to verify each other's certificates. When a user uses a personal assistant, the personal assistant authenticates the user's identity using the OIDC protocol, and the user verifies the personal assistant's identity using the TLS 1.3 protocol.

## 5.6. Agent Discovery

For agent interconnection to become a secure and reliable agent system, a standardized and flexible agent discovery process is needed to enable collaboration among agents. This protocol defines the Agent Discovery Protocol (ADP), which follows the principles below and achieves the corresponding goals:

(1) Satisfy the collaboration requirements of heterogeneous agents: a service-requesting agent can quickly discover agents that meet its capability requirements through the ADP mechanism;

(2) Support autonomous collaboration in dynamic environments: the ADP mechanism should be able to adapt to the highly dynamic nature of agent interconnection caused by agents joining, leaving, or changing, ensuring that the service-requesting agent can obtain agents that meet its capability requirements;

### 5.6.1 Role Definitions

● Requesting agent: The initiator of a service request that, according to task goals, needs other agents to provide services for collaboration.

● Discovery agent: An agent provided by an agent discovery service provider whose function is to receive agent discovery requests and return matching retrieval results. Information about the discovery agent can be obtained through configuration in the environment where the requesting agent resides (similar to DNS server configuration), or by the requesting agent querying the agent registration service provider with which it is registered.

● Registration agent: An agent provided by the agent registration server whose function is to provide agent registration services externally.

● Serving agent: An agent created and managed by an agent provider that provides services.

### 5.6.2 Agent Discovery Process

(1) The requesting agent reasons about the task;

(2) The requesting agent decomposes the tasks that need to be completed in collaboration with other agents, sends an agent discovery request to the discovery agent, and carries the information of the tasks that need collaboration;

(3) The registration agent sends agent description information to the discovery agent, and the discovery agent stores this information; for information synchronization between the registration agent and the discovery agent, refer to [ACPs-spec-DSP](../08-ACPs-spec-DSP/ACPs-spec-DSP_en.md);

(4) The discovery agent matches the information of the tasks that need collaboration in the request against the stored agent description information;

(5) The information of the agent or agent list that meets the requirements is returned to the requesting agent;

(6) According to its own policy, the requesting agent selects the agents it needs to collaborate with (serving agents) from the list and performs identity verification with them;

(7) After identity verification succeeds, the requesting agent establishes connections with the serving agents and collaborates to complete the task.

## 5.7. Agent Interaction

For agent interconnection to become a secure and reliable agent system, a standardized set of processes and protocols is needed to support interaction among agents. To build a powerful, extensible, and practical multi-agent collaboration environment, the Agent Interaction Protocol (AIP) in the ACPs protocol suite focuses on interaction among agents, with the following key goals:

(1) Collaboration: Provide standardized mechanisms to promote in-depth collaboration among agents, enabling agents to proactively delegate tasks (assigning subtasks to more suitable agents) and to exchange context information efficiently (sharing environment awareness, goal states, knowledge, and so on).

(2) Interoperability: By defining standardized information formats, semantics, and interaction modes, shield the differences of underlying agent platforms, programming languages, or internal implementations, so that heterogeneous agent systems developed by different teams, running in different environments, and possessing different capabilities can seamlessly discover one another, understand the interaction content, and collaborate effectively.

(3) Flexibility: Support diverse interaction requirements without imposing a single interaction mode, but rather allowing agents to choose the most suitable interaction mode according to the scenario, and to adapt to different information payloads and quality-of-service requirements.

(4) Asynchrony: Support long-running tasks, ensuring that requests, intermediate state updates, and final results can be delivered reliably and on demand. After an agent initiates a request (such as delegating a time-consuming task), it does not need to keep waiting for a response and can immediately handle other matters.

### 5.7.1 Role Definitions

In the agent interaction process, there are the following two roles:

(1) Leader: In agent interaction, the leader is the agent that publishes tasks and organizes the interaction. In one complete interaction, there can be only one leader.

(2) Partner: In agent interaction, a partner is an agent that accepts tasks and provides services. After accepting a task from the leader, a partner executes it and returns the execution result.

### 5.7.2 Interaction Modes

In the Agent Interaction Protocol, there are three interaction modes between Leader and Partner:

(1) Direct Interaction Mode: In direct interaction mode, the Leader creates and maintains a Session, and the relevant agents collaborate to complete the task within the same Session. In this Session, the Leader interacts directly with each Partner, and there is no interaction among Partners. The interaction between the Leader and a Partner can be implemented in three ways: remote invocation, streaming, and asynchronous notification.

(2) Grouping Interaction Mode: In grouping interaction mode, the Leader creates and maintains a Session, and the interaction messages among agents in that Session are distributed through a message queue (Message Queue). After creating the Session, the Leader invites the relevant Partners to join the group and subscribe to the information in that Session from the message queue. The interaction messages between the Leader and the Partners are then sent to the message queue and distributed by it. All agents in the same group can send and receive messages through the message distribution module.

(3) Hybrid Interaction Mode: In hybrid interaction mode, the Leader creates and maintains a Session, and within that Session the Leader can interact with Partners both in direct interaction mode and in grouping interaction mode.

### 5.7.3 Interaction Network

In agent interaction scenarios, agents establish connections with other agents in the two modes of direct connection and grouping. Some agents can belong to different groups at the same time and reuse message queue services, thereby forming a dynamic agent interaction network. The agent interaction structure at a given moment is shown in the figure below:

![Interaction network](./1-2.jpg)

## 5.8. Data Synchronization Protocol

### 5.8.1 Role Definitions

- **Provider**
The source of the data, which needs to maintain the integrity, legitimacy, and consistency of the data and provide a reliable data baseline for consumers.

- **Consumer**
Relies on the data provided by the provider and is responsible for consuming and storing it locally.

### 5.8.2 Synchronization Methods

The data synchronization methods between the data provider (Provider) and the data consumer (Consumer) are as follows:

(1) When the data consumer starts for the first time, loses data, or lags far behind the data provider for a long time, it should first call the capability negotiation interface (Info API) once to obtain the provider's running status and key configuration, and choose an appropriate synchronization strategy accordingly; the data consumer then actively requests a full/incremental snapshot (Snapshot) once to complete the initial alignment of data.

(2) After alignment is complete, normal operation begins, that is, the data consumer continuously polls the data provider in incremental mode (Changes), pulling only newly added or changed data to ensure real-time consistency. If higher timeliness of data synchronization is required, the data provider can actively push change notifications (Webhook Notification), thereby reducing the query pressure on the discovery side.

(3) If the connection is unexpectedly interrupted for a long time, the data consumer should again evaluate the current retention window and service status through the Info API, and when necessary re-pull a full or incremental snapshot to realign the data and ensure that both sides remain synchronized.

# 6. Supplementary Notes

This document introduces the agent collaboration protocol suite (Agent Collaboration Protocols, ACPs) for agent interconnection proposed and preliminarily defined by the BUPT Agent Interconnection Research Group. The ACPs protocol suite is proposed from the perspective that agent interconnection will become critical network infrastructure in the future, attempting to provide new ideas and methods for the solid development of agent interconnection from a more holistic vision. It should be particularly noted that this document is the research team's first exposition of the basic framework and ideas of the ACPs protocol suite, and there may still be certain shortcomings at the level of concrete implementation. We welcome readers' corrections and discussion so that we can improve the protocol suite together. In follow-up work, we will further refine the ACPs framework and set out the implementation details and reference implementations of the ACPs protocol suite.
