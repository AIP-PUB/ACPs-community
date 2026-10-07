# ACPs Project Overview
For the complete project repository, please visit: https://github.com/AIP-PUB/ACPs-community

## 1. Project Introduction
This project is part of the open-source ecosystem for agent interconnection and Internet of Agents (IoA). Led by Beijing University of Posts and Telecommunications (BUPT) and developed with the support of the China Electronics Standardization Institute (CESI), the project released version v1.0.0 in November 2025.

Organizations responsible for document preparation:
School of Artificial Intelligence, Beijing University of Posts and Telecommunications; China Electronics Standardization Institute

Document authors:
Jun Liu (Beijing University of Posts and Telecommunications), Ge Gao (China Electronics Standardization Institute), Ke Li (Beijing University of Posts and Telecommunications), Keliang Chen (Beijing University of Posts and Telecommunications), Ke Yu (Beijing University of Posts and Telecommunications), Xiaofeng Hu (Beijing University of Posts and Telecommunications), Di Ma (Beijing University of Posts and Telecommunications).

## 2. Protocol Overview
With the rapid development of artificial intelligence, agents have become a key vehicle for transforming AI from a conceptual technology into practical productive capabilities. Their applications are expanding rapidly across various fields, playing an increasingly important role in enabling new industrialization and fostering new quality productive forces. However, the development of the agent industry currently faces numerous challenges. Different agents encounter difficulties in achieving interconnection, interoperability, and interoperation, while issues such as data silos, heterogeneous communication barriers, and the lack of collaboration standards constrain the realization of their full application potential and the improvement of industrial competitiveness.

To systematically address these challenges, guide and standardize the development of agent interconnection technologies, and improve the interoperability, composability, and overall industrial efficiency of agent systems, this guiding technical document has been formulated. GB/Z 185 Artificial Intelligence — Agent Interconnection aims to specify the technical requirements and processes for agent interconnection. Its development follows the principles of systematicity, advancement, and operability, providing a unified technical framework and standard basis for achieving cross-platform and cross-architecture interconnection, communication, and interoperability among agents. GB/Z 185 is planned to consist of seven parts:

- Part 1: General Architecture. Defines the conceptual model and functional model of the agent interconnection environment.

- Part 2: Identity Code. Defines agent identity codes and their applications, and provides recommendations for agent code structures and allocation principles.

- Part 3: Identity Management. Defines the identity management framework and full lifecycle processes in an agent interconnection environment, and describes the technical requirements for identity management.

- Part 4: Agent Description. Defines methods for describing agents and provides reference processes for agent description registration, modification, and publication.

- Part 5: Agent Discovery. Provides a reference discovery process for agent interconnection.

- Part 6: Agent Interaction. Defines interaction patterns for large-scale agent interconnection and describes the basic elements and interface definitions of agent interactions.

- Part 7: Agent Tool Calling. Defines a standardized architecture, process, and tool description for LLM-based agent tool calling, supporting seamless integration between agents and external tools.

GB/Z 185 Artificial Intelligence — Agent Interconnection was officially published on May 22, 2026. This project provides protocol references and code implementations based on GB/Z 185 Artificial Intelligence — Agent Interconnection.

3## . Agent Collaboration Protocols
The Agent Collaboration Protocols (ACPs) is a standardized interaction protocol suite designed and implemented based on AIP to enable efficient collaboration among heterogeneous agents and support diverse agent interconnection applications.

For detailed specifications, please refer to the ACPs protocol specification: ACPs-community/acps-specs.

## 4. Supporting Open-Source Implementations
This project also provides open-source reference implementations for the Agent Collaboration Protocols, including:

```text
ACPs-community/
|-- README.md
|-- acps-specs/             ............. ACPs protocol specifications
|-- acps-sdk/               ............. ACPs SDK, providing agent developers with convenient
|                                      support for agent interconnection and collaboration
|-- acps-cli/               ............. ACPs unified command-line toolset
|-- acps-docs/              ............. ACPs reference documentation
|-- acps-infra/             ............. ACPs infrastructure and deployment orchestration repository
|-- ca-server/              ............. ACPs ATR agent CA certification service
|-- registry-server/        ............. ACPs ATR agent registration service
|-- discovery-server/       ............. ACPs ADP discovery service implementation
|-- mq-auth-server/         ............. Authentication and group ACL service for group-based
|                                      interaction, providing an HTTP auth backend for MQ
|-- demo-leader/            ............. ACPs-leader agent example
`-- demo-partner/           ............. ACPs-partner agent example
```

## 5. Version History
- November 1, 2025
Released v1.0.0.

```text
1. Released protocol specification v1.0.0.
2. Implemented trusted agent registration.
3. Implemented agent discovery.
4. Implemented trusted agent interconnection.
5. The demo project demonstrates how to develop an interconnected
   agent application based on the ACPs protocol.
```

- March 12, 2026
Released v2.0.0.

```text
1. Introduced the OID format for AIC agent identity codes, providing global uniqueness.
2. Improved ACS:
   - Added support for individual developers.
   - Added support for distinguishing entities within an ontology.
   - Adjusted certain fields to improve flexibility.
3. Improved the ATR protocol to support the new AIC/ACS specifications.
4. Improved ATR components:
   - Improved the certificate service (ACPs-CA-Server).
   - Rewrote the ATR client (ACPs-CA-Client).
   - Rewrote the ATR challenge service (added the ACPs-CA-Challenge repository).
5. Improved the AIP protocol:
   - Restructured data objects to make them easier to understand and extend.
   - Added sample code implementing the group interaction model.
6. Improved the ADP protocol:
   - Supports multiple matching modes, including explicit-intent matching,
     exploratory-intent matching, and structured filtering.
   - Supports cross-domain extensions, including request forwarding and
     control for discovery services.
   - The discovery result data structure supports task decomposition.
7. Open-source SDK: added the ACPs-SDK repository:
   - Support for the new AIC protocol and AIC validity verification tools.
   - ACS specification implementation.
   - AIP protocol implementation.
   - ADP protocol implementation.
8. Added a new example application for agent interconnection based on ACPs,
   using a decoupled architecture of a general-purpose foundation layer
   plus scenario-specific plugins.
```

- June 22, 2026
Released v2.1.0.

```text
1. Removed the challenge step from the certificate application process.
   Instead, certificate applications use EAB credentials bound to the
   Agent Provider account and AIC, simplifying the infrastructure and
   application process while improving automation.

2. Added registration and automatic approval APIs for derived entities
   based on mTLS verification of the parent entity certificate,
   further improving automation.

3. Added an agent interconnection mechanism based on message-queue
   Inboxes. Message queues are used to handle group invitations and
   membership, eliminating the need to expose a JSON-RPC interface
   externally and making the mechanism suitable for agent deployments
   in private networks.

4. Added mandatory requirements for code formatting and code quality.

5. Enhanced test cases.

6. Added Docker-based packaging and deployment, suitable for development,
   validation, and testing in single-machine environments.

7. Added a general-purpose packaging and deployment approach suitable
   for customized production environments.

8. Merged all repositories into ACPs-community. The corresponding mapping is:

   - Agent-Interconnection-Protocol-Project -> ACPs-community/acps-specs
   - ACPs-SDK                               -> ACPs-community/acps-sdk
   - ACPs-Registry-Server                   -> ACPs-community/registry-server
   - ACPs-CA-Server                         -> ACPs-community/ca-server
   - ACPs-CA-Client                         -> Removed; functionality migrated to acps-cli
   - ACPs-CA-Challenge                      -> Removed; replaced by EAB credential verification
   - ACPs-Discovery-Server                  -> ACPs-community/discovery-server
   - ACPs-Demo-Project                      -> Split into:
                                                - ACPs-community/demo-leader
                                                - ACPs-community/demo-partner

9. Added new modules to support the improvements above:

   - Added ACPs-community/acps-infra as general-purpose infrastructure.
   - Added ACPs-community/mq-auth-server to support authentication
     and ACL control for message queues.
   - Added ACPs-community/acps-docs to provide more detailed manuals
     and documentation.
   - Added ACPs-community/acps-cli to integrate client-side tools,
     supporting registry, CA, discovery, MQ authentication, and more.
```

## 6. Getting Started
To help developers get started quickly, we provide two detailed guides. Developers can refer to the appropriate guide according to their development needs:

- [GettingStarted](https://github.com/AIP-PUB/ACPs-community/blob/main/acps-docs/getting-started/README_en.md)
Agent Platform Development Guide — intended for developers building agent interconnection platforms and integrating the ACPs protocol. It guides developers through platform-level development and configuration.

- [Tutorials](https://github.com/AIP-PUB/ACPs-community/blob/main/acps-docs/tutorials/agent-development.md)
Agent Integration Tutorials — provide guidance on connecting individual agents to ACPs, helping developers build agents compliant with the ACPs specifications and quickly integrate them with the ACPs ecosystem.

## 7. More Documentation
> See [`ACPs-community/acps-docs`](https://github.com/AIP-PUB/ACPs-community/tree/main/acps-docs)

## 8. Demos
To help users quickly understand agent interconnection, we provide agent interconnection examples based on a Beijing travel scenario. The demos demonstrate how multiple agents can collaborate to complete a comprehensive travel planning task covering attractions, dining, accommodation, transportation, and other aspects of a trip to Beijing.

- Leader agent example: See `ACPs-community/demo-leader`
The leader agent assists with tasks, interacts with users, and coordinates multi-agent task collaboration based on the ACPs protocol.

- Partner agent examples: See `ACPs-community/demo-partner`
This includes five specialized agents responsible for Beijing urban attractions, Beijing suburban attractions, Beijing food recommendations, nationwide hotel arrangements, and nationwide transportation arrangements. Under the coordination of the leader agent, these five specialized agents collaborate through the ACPs protocol to complete a comprehensive Beijing travel itinerary planning and recommendation task.

## 9. Additional Notes
This project introduces the Agent Collaboration Protocols (ACPs), a protocol suite for large-scale agent interconnection and collaboration, proposed and initially defined by the Agent Interconnection Research Group of the School of Artificial Intelligence at Beijing University of Posts and Telecommunications (Jun Liu, Ke Li, Keliang Chen, Ke Yu, Xiaofeng Hu, and Di Ma).

ACPs is developed from the perspective that agent interconnection will become a critical network infrastructure in the future. It attempts to provide new ideas and approaches for the robust development of agent interconnection from a more comprehensive and global perspective.

It should be noted that the framework and concepts of the protocol suite are still under continuous improvement, and the current implementation may have certain limitations. We welcome feedback, discussion, and contributions from technology enthusiasts and the broader developer community to jointly improve and advance this protocol suite.
