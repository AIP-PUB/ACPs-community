[Home](../README_en.md)

**[English](ACPs-spec-AIC_en.md) | [中文](ACPs-spec-AIC.md)**

AIC: Agent Identity Code Definition (ACPs-spec-AIC-v02.02)

# 1. Document Definition

This document is the standard definition of the Agent Identity Code (AIC) in the ACPs agent collaboration protocol suite, version v02.02.

The full title of the document is ACPs-spec-AIC-v02.02.

Document authors: Ke Li (Beijing University of Posts and Telecommunications), Ke Yu (Beijing University of Posts and Telecommunications), Jun Liu (Beijing University of Posts and Telecommunications), Haozhe Song (Beijing University of Posts and Telecommunications), Xiaolian Guo (Beijing University of Posts and Telecommunications), Yinming Li (Beijing University of Posts and Telecommunications), Xiaofeng Hu (Beijing University of Posts and Telecommunications), Di Ma (Beijing University of Posts and Telecommunications), Keliang Chen (Beijing University of Posts and Telecommunications), Ge Gao (China Electronics Standardization Institute).

# 2. Introduction to the Agent Identity Code and Related Processes

For agent interconnection to become a secure and reliable agent system, the primary condition is that the agents running within it with the ability to execute tasks autonomously should be secure and reliable instances. To meet this condition, each agent should have an authenticatable identity identifier, which we define as the Agent Identity Code (AIC).

Each agent should have a unique AIC, which comes from the agent registration service provider (ARSP) with which the agent first registers. In agent interconnection, there may be multiple agent registration service providers, and each provider should be a service entity that has been recognized by consensus (for example, certified by a management authority).

# 3. Definition of the Agent Identity Code

The Agent Identity Code (AIC) consists of two major parts: the identity code prefix and the identity code content. The Agent Identity Code prefix is uniformly allocated and managed by the national OID registration authority, and serves as the root identifier of the Agent Identity Code in the global identification system and the starting point for resolving agent identity information.

The Agent Identity Code consists of multiple levels of identifiers in sequence, separated by the `.` symbol. Each identifier uses Arabic numerals or English letters, where Arabic numerals range from 0 to 9 and English letters range from A to Z (case-insensitive; uppercase is recommended). The following is an example AIC:

```
1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
```

Here 1.2.156.3088 is the Agent Identity Code prefix, and 1.1.34C2.478BDF.3GF546.0JU4 is the Agent Identity Code content. Explanation:

**Identity code prefix**
- (1) Top-level arc: 1 indicates ISO.
- (2) Second-level arc: 2 indicates a national member body.
- (3) Level 3: 156 indicates China.
- (4) Level 4: The dedicated node for agent interconnection approved by the national OID registration authority.

**Identity code content**
- (5) Level 5: Agent Identity Code version number (value range 1–Z). 1 indicates that the Agent Identity Code version is 1.
- (6) Level 6: Agent registration service provider index (value range 1–ZZZZZZ). 1 indicates one agent registration service provider A.
- (7) Level 7: Agent provider index (value range 1–ZZZZZZ). 34C2 indicates the identifier assigned by agent registration service provider A to agent provider B.
- (8) Level 8: Agent ontology serial number (value range 1–ZZZZZZZZZ). 478BDF indicates an agent ontology serial number identifier assigned by agent registration service provider A to agent provider B.
- (9) Level 9: Agent entity serial number (value range 0–ZZZZZZZZZ). 3GF546 indicates the agent entity serial number identifier created from the agent ontology with serial number 478BDF. When the registration object is an agent ontology, its agent entity serial number is identified by the special character 0.
- (10) Level 10: Checksum (value range 0000–1EKF). In the example the checksum is 0JU4. How the checksum is generated and verified is explained in Section 4.

# 4. Checksum Calculation and Usage

## 4.1 How the Checksum Is Calculated

The checksum of the Agent Identity Code (Level 10) is computed from the content of the preceding data code (Levels 1–9) (neither the data code nor the checksum includes the separator `.` between Level 9 and Level 10). For:

1.2.156.3088.1.1.34C2.478BDF.3GF546

its checksum is computed using [AUTOSAR CRC-16/CCITT-FALSE](https://www.autosar.org/fileadmin/standards/R23-11/CP/AUTOSAR_CP_SWS_CRCLibrary.pdf) with the following parameters: the generator polynomial is 0x1021, the initial value is 0xFFFF, input and output are not reflected, and there is no final XOR (xorout = 0x0000). The calculation process is as follows:

(1) First convert all letters to uppercase, then encode them as a byte sequence according to ASCII/UTF-8 to serve as the input data of the `CRC`.
For example: the input data is the ASCII byte stream of the string `1.2.156.3088.1.1.34C2.478BDF.3GF546`. (Note: at this point `.` is also part of the input bytes.)

(2) Before performing the CRC calculation, introduce a **salt value (Salt)**. The salt value is a fixed byte sequence maintained internally by each agent registration service provider, with a length of no less than 2 bytes. Append it after the ASCII byte stream obtained in (1) to form the final CRC input byte sequence.
For example: take a 2-byte salt value SALT = 0x1234 (byte sequence 0x12, 0x34); then the final input is:
ASCII("1.2.156.3088.1.1.34C2.478BDF.3GF546") + [0x12, 0x34]

(3) Initialize the 16-bit register `CRC` to 0xFFFF.

(4) Process the input data byte by byte. For each byte `B`, perform:
`CRC = CRC XOR (B << 8)` (that is, XOR this byte into the upper 8 bits of `CRC`).
For example: the ASCII code of the letter "B" is 0x42 (decimal 66, binary 0100 0010B). In this algorithm the byte must be shifted left by 8 bits and then XORed with the CRC, that is: `B << 8 = 0x42 << 8 = 0x4200`. Since the current initial value of `CRC` is 0xFFFF, performing the XOR gives:
`CRC = 0xFFFF XOR 0x4200 = 0xBDFF`. Here 0xFFFF is 1111 1111 1111 1111B in binary and 0x4200 is 0100 0010 0000 0000B in binary; after the XOR we get 0xBDFF (1011 1101 1111 1111B).

(5) For each byte, perform 8 bitwise iterations (corresponding to the 8 bits of that byte). In each iteration, update `CRC` according to the following rules:

- If the most significant bit of `CRC` (0x8000) is 1, then `CRC = (CRC << 1) XOR 0x1021`;

- Otherwise `CRC = (CRC << 1)`; and always keep `CRC` at 16 bits (discard the overflowing part and keep only the lower 16 bits).

(6) After all bytes have been processed, the final 16-bit `CRC` value is obtained, which is the **raw checksum value**. Its value range is 0x0000 to 0xFFFF.
For example: for 1.2.156.3088.1.1.34C2.478BDF.3GF546 with the salt value `SALT = 0x1234`, the raw `CRC` checksum value is calculated to be 0x646C.

(7) Convert the raw checksum value using the Base36 encoding rules: the character set is 0-9 and A-Z (36 characters in total, where 0 maps to 0, 9 maps to 9, 10 maps to A, and 35 maps to Z). After conversion, format it as a fixed 4-character Base36 string, padding with 0 on the left if it is shorter than 4 characters. This 4-character Base36 string is the checksum (Level 10).
For example: the raw checksum value 0x646C is converted by Base36 encoding to 0JU4, so the final checksum is 0JU4.

(8) Append the checksum as Level 10 after the data code and add the separator `.` to form the complete identity code.
For example: the complete identity code is 1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4

## 4.2 Usage of the Checksum

Whether an Agent Identity Code is valid can be verified as follows:

(1) Divide the identity code into two parts, the data code and the checksum: the data code is Levels 1–9 and the checksum is Level 10 (ignoring the separator `.` between Level 9 and Level 10).
For example: divide 1.2.156.3088.1.1.34C2.478bDF.3GF546.0JU4 into:
data code 1.2.156.3088.1.1.34C2.478bDF.3GF546 and checksum 0JU4.

(2) Apply the same normalization to the data code as was used during calculation, converting all letters to uppercase.
For example: the normalized data code is 1.2.156.3088.1.1.34C2.478BDF.3GF546.

(3) In the same way as during calculation, append the salt byte sequence corresponding to this ARSP to the end of the normalized ASCII byte stream, and then compute the raw checksum value with the same CRC parameters.
For example: the recomputed raw checksum value is 0x646C.

(4) Convert the recomputed raw checksum value into a fixed-length 4-character Base36 string using the same Base36 encoding rules (padding with 0 if shorter).
For example: the raw checksum value 0x646C → after Base36 conversion gives 0JU4.

(5) Compare the recomputed checksum with the checksum carried in the identity code (Level 10). If the two agree, verification succeeds; otherwise the identity code is invalid.
For example: the recomputed value is 0JU4, which agrees with the carried 0JU4, so the identity code is valid.

# 5. Supplementary Notes

The AIC defined in this document fully takes manageability and compatibility into consideration and is provided free of charge to relevant developers and institutions for reference. We welcome other industry colleagues engaged in agent development and the formulation of agent interconnection protocols to support and adopt this AIC definition, so as to form an Agent Identity Code definition that favors interconnection and good compatibility.
