//! RFC 3711 vectors and one worked protection run — the numbers `src/srtp.rs`
//! points here for.
//!
//! What the RFC actually publishes is **§B.2** (AES-CM keystream, the counter
//! mode SRTP encrypts with) and **§B.3** (key derivation, including the AES-CM
//! input blocks and their outputs). Appendix B ends at B.3: there is no §B.4,
//! so the "example protection run" is *reconstructed* here from those vectors
//! plus the RFC's own formulas —
//!
//! * the frame is §B.1's example RTP packet (its printed header and payload),
//! * the keying is §B.3's example master key + salt,
//! * the per-packet IV is §4.1.1's `IV = (k_s*2^16) XOR (SSRC*2^64) XOR (i*2^16)`,
//! * the tag is §4.2.1's HMAC-SHA1 over `packet || ROC`, truncated to 80 bits,
//! * the index `i = ROC*2^16 + SEQ` and the ROC wrap rule are Appendix A's.
//!
//! Because the RFC never combines §B.1's packet with §B.3's keys, no published
//! ciphertext exists for this pairing. So the run is anchored twice: the
//! published §B.2/§B.3 numbers pin the primitives, and every intermediate in
//! the run is **recomputed in this file from the RFC's formulas** and compared
//! against the library's output. Two implementations of the same formula
//! disagreeing is the failure mode this catches (the library's `packet_iv`
//! is private and is deliberately not used by the arithmetic below).
//!
//! `tests/web.rs` already covers round-trip, replay, out-of-order delivery and
//! the RFC 7714 GCM profiles. Everything here is additive: vectors and the
//! worked run, not a second copy of that coverage.

use webrtc::crypto::{aes128::Aes128, hmac::hmac_sha1};
use webrtc::srtp::{
    derive_session_keys, ProtectError, SessionKeys, SrtpProtector, SrtpUnprotector, AUTH_TAG_LEN,
};

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn unhex(s: &str) -> Vec<u8> {
    assert!(s.len().is_multiple_of(2), "hex literal has an odd length");
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).expect("hex literal"))
        .collect()
}

// ============================================================ RFC 3711 §B.3

/// The example master key and salt §B.3 derives everything from.
const MASTER_KEY: &str = "e1f97a0d3e018be0d64fa32c06de4139";
const MASTER_SALT: &str = "0ec675ad498afeebb6960b3aabe6";

fn rfc_master_key() -> [u8; 16] {
    let v = unhex(MASTER_KEY);
    v.try_into().expect("16-byte master key")
}

fn rfc_master_salt() -> [u8; 14] {
    let v = unhex(MASTER_SALT);
    v.try_into().expect("14-byte master salt")
}

#[test]
fn rfc3711_b3_derives_the_three_session_keys() {
    let keys = derive_session_keys(&rfc_master_key(), &rfc_master_salt());

    // §B.3, "cipher key", "cipher salt" and the auth-key block table, byte for
    // byte as printed (the auth key is 94 octets there; the RFC 3711 default
    // profile uses the first 20 of them).
    assert_eq!(hex(&keys.aes), "c61e7a93744f39ee10734afe3ff7a087");
    assert_eq!(hex(&keys.salt), "30cbbc08863d8c85d49db34a9ae1");
    assert_eq!(hex(&keys.auth), "cebe321f6ff7716b6fd4ab49af256a156d38baa4");
}

#[test]
fn rfc3711_b3_prints_the_aes_cm_input_blocks_the_kdf_walks() {
    // §B.3 lists the intermediate AES-CM inputs as well as the outputs. Those
    // inputs are what the label/index XOR produces, so encrypting them with
    // the master key through our own AES-128 must reproduce every published
    // output — and the outputs must equal what `derive_session_keys` returns.
    // A drift in the derivation (label position, counter placement, the
    // multiply-by-2^16 padding) shows up here as a mismatch between the two.
    let aes = Aes128::new(&rfc_master_key());
    let keys = derive_session_keys(&rfc_master_key(), &rfc_master_salt());

    // label 0x00 (encryption) at index 0.
    let cipher_in = unhex("0ec675ad498afeebb6960b3aabe60000");
    let cipher_out = aes.encrypt_block(&cipher_in.try_into().unwrap());
    assert_eq!(hex(&cipher_out), "c61e7a93744f39ee10734afe3ff7a087");
    assert_eq!(cipher_out, keys.aes);

    // label 0x02 (salt): the RFC prints a 16-byte output of which the first
    // 14 octets are the session salt.
    let salt_in = unhex("0ec675ad498afee9b6960b3aabe60000");
    let salt_out = aes.encrypt_block(&salt_in.try_into().unwrap());
    assert_eq!(hex(&salt_out), "30cbbc08863d8c85d49db34a9ae17ac6");
    assert_eq!(&salt_out[..14], &keys.salt[..]);

    // label 0x01 (authentication): six consecutive blocks, 94 octets total.
    let auth_blocks = [
        (
            "0ec675ad498afeeab6960b3aabe60000",
            "cebe321f6ff7716b6fd4ab49af256a15",
        ),
        (
            "0ec675ad498afeeab6960b3aabe60001",
            "6d38baa48f0a0acf3c34e2359e6cdbce",
        ),
        (
            "0ec675ad498afeeab6960b3aabe60002",
            "e049646c43d9327ad175578ef7227098",
        ),
        (
            "0ec675ad498afeeab6960b3aabe60003",
            "6371c10c9a369ac2f94a8c5fbcdddc25",
        ),
        (
            "0ec675ad498afeeab6960b3aabe60004",
            "6d6e919a48b610ef17c2041e47403576",
        ),
        // The last block is 14 octets: 5*16 + 14 = 94.
        (
            "0ec675ad498afeeab6960b3aabe60005",
            "6b68642c59bbfc2f34db60dbdfb2",
        ),
    ];
    let mut auth = Vec::new();
    for (input, expected) in auth_blocks {
        let out = aes.encrypt_block(&unhex(input).try_into().unwrap());
        // The final block contributes 14 of its 16 octets: 5*16 + 14 = 94.
        let used = expected.len() / 2;
        assert_eq!(
            hex(&out[..used]),
            expected,
            "auth key block for input {input}"
        );
        auth.extend_from_slice(&out[..used]);
    }
    assert_eq!(auth.len(), 94, "§B.3's auth key is 94 octets");
    assert_eq!(&auth[..keys.auth.len()], &keys.auth[..]);
}

// ============================================================ RFC 3711 §B.2

/// §B.2: the AES-CM keystream segment for the CTR-mode test key, starting at
/// the "already shifted" salt offset. `Aes128::apply_keystream` XORs its input
/// in place, so running it over zeros yields the keystream itself.
#[test]
fn rfc3711_b2_aes_cm_keystream_vectors() {
    let key: [u8; 16] = unhex("2b7e151628aed2a6abf7158809cf4f3c")
        .try_into()
        .unwrap();
    let offset: [u8; 16] = unhex("f0f1f2f3f4f5f6f7f8f9fafbfcfd0000")
        .try_into()
        .unwrap();
    let aes = Aes128::new(&key);

    // First three blocks of the segment.
    let mut head = [0u8; 48];
    aes.apply_keystream(&offset, &mut head, 0);
    assert_eq!(hex(&head[0..16]), "e03ead0935c95e80e166b16dd92b4eb4");
    assert_eq!(hex(&head[16..32]), "d23513162b02d0f72a43a2fe4a5f97ab");
    assert_eq!(hex(&head[32..48]), "41e95b3bb0a2e8dd477901e4fca894c0");

    // The tail of the 1044512-octet segment, reached through `byte_offset`
    // rather than by encrypting a megabyte: counter F0F1...FDFF00 is block
    // 0xFF00, FDFF01 is the last block of the segment (65282 blocks).
    let mut tail = [0u8; 32];
    aes.apply_keystream(&offset, &mut tail, 0xFF00 * 16);
    assert_eq!(hex(&tail[0..16]), "362b7c3c6773516318a077d7fc5073ae");
    assert_eq!(hex(&tail[16..32]), "6a2cc3787889374fbeb4c81b17ba6c44");

    // ...and the block that precedes them, to prove the offset is a plain
    // counter continuation rather than a special case at the segment end.
    let mut prev = [0u8; 16];
    aes.apply_keystream(&offset, &mut prev, 0xFEFF * 16);
    assert_eq!(hex(&prev), "ec8cdf7398607cb0f2d21675ea9ea1e4");

    // A run that starts mid-block must produce the tail of one block followed
    // by the next: offset 17 is the third byte of ...FD0000 through the second
    // byte of ...FD0001. SRTP never starts mid-block, but this is the exact
    // arithmetic the per-packet IV relies on (index bytes land at the tail of
    // the IV), so the partial-block path is pinned here against §B.2's bytes.
    let mut mid = [0u8; 15];
    aes.apply_keystream(&offset, &mut mid, 2);
    // octets 2..16 of the ...FD0000 block, then octet 0 of ...FD0001.
    assert_eq!(hex(&mid), "ad0935c95e80e166b16dd92b4eb4d2");
}

// =================================================== RFC 3711 worked run

/// §B.1's example RTP packet: the printed header bytes and payload.
const B1_HEADER: &str = "806e5cba50681de55c621599";
const B1_PAYLOAD: &str =
    "70736575646f72616e646f6d6e65737320697320746865206e6578742062657374207468696e67";

fn example_packet(seq: u16) -> streams::packet::RtpPacket {
    // The header decodes to V=2, PT=0x6e (110), and the printed timestamp and
    // SSRC; the sequence is a parameter so the same frame can be replayed
    // across the wrap cases below. With `seq == 0x5cba` this is §B.1's frame
    // unmodified, which the assertion below spells out.
    let payload = unhex(B1_PAYLOAD);
    let template = unhex(B1_HEADER);
    let packet = streams::packet::RtpPacket::build(
        template[1] & 0x7f,
        seq,
        u32::from_be_bytes([template[4], template[5], template[6], template[7]]),
        u32::from_be_bytes([template[8], template[9], template[10], template[11]]),
        template[1] & 0x80 != 0,
        &payload,
    );
    let mut expected_header = template;
    expected_header[2..4].copy_from_slice(&seq.to_be_bytes());
    assert_eq!(packet.raw[..12], expected_header[..]);
    assert_eq!(packet.raw[12..], payload[..]);
    assert_eq!(packet.payload_type, 0x6e);
    assert_eq!(packet.ssrc, 0x5c621599);
    packet
}

/// Recompute, from the RFC's formulas alone, what an SRTP protection run of
/// `packet` under `keys` must produce at the given ROC.
///
/// This is the test-side implementation: §4.1.1's IV, §4.1's AES-CM payload
/// encryption, §4.2.1's `HMAC-SHA1(packet || ROC)` truncated to 80 bits. It
/// shares no code with `srtp.rs` except the public AES/HMAC primitives, so an
/// agreement is evidence and a disagreement is a bug in one of the two.
fn rfc_protection_run(
    packet: &streams::packet::RtpPacket,
    keys: &SessionKeys,
    roc: u32,
) -> Vec<u8> {
    let mut out = packet.raw.clone();

    // §4.1.1: IV = (k_s * 2^16) XOR (SSRC * 2^64) XOR (i * 2^16), i.e. the
    // 112-bit salt with the SSRC in octets 4..8 and the 48-bit index
    // (ROC || SEQ) in octets 8..14, then the 16-bit block counter at the tail.
    let mut iv = [0u8; 16];
    iv[..14].copy_from_slice(&keys.salt);
    for (i, b) in packet.ssrc.to_be_bytes().iter().enumerate() {
        iv[4 + i] ^= b;
    }
    let index: u64 = (u64::from(roc) << 16) | u64::from(packet.sequence);
    for (i, b) in index.to_be_bytes()[2..].iter().enumerate() {
        iv[8 + i] ^= b;
    }

    let aes = Aes128::new(&keys.aes);
    aes.apply_keystream(&iv, &mut out[12..], 0);

    // §4.2.1: the MAC covers the encrypted packet plus the ROC.
    let mut mac_input = out.clone();
    mac_input.extend_from_slice(&roc.to_be_bytes());
    let tag = hmac_sha1(&keys.auth, &mac_input);
    out.extend_from_slice(&tag[..AUTH_TAG_LEN]);
    out
}

#[test]
fn rfc3711_example_protection_run_matches_the_formulas() {
    let keys = derive_session_keys(&rfc_master_key(), &rfc_master_salt());
    let packet = example_packet(0x5cba);
    let mut tx = SrtpProtector::new(keys.clone());

    let wire = tx.protect(&packet);

    // Structure: header in the clear, payload encrypted, 80-bit tag appended.
    assert_eq!(wire.len(), packet.raw.len() + AUTH_TAG_LEN);
    assert_eq!(
        &wire[..12],
        &packet.raw[..12],
        "RTP header stays in the clear"
    );
    assert_ne!(
        &wire[12..packet.raw.len()],
        &packet.raw[12..],
        "the payload must not be sent in the clear"
    );

    // The whole datagram, byte for byte, against the test-side computation of
    // §4.1.1 + §4.2.1 with the §B.3 session keys. First packet in the stream,
    // so the index is the printed sequence with ROC 0.
    let expected = rfc_protection_run(&packet, &keys, 0);
    assert_eq!(hex(&wire), hex(&expected));

    // And the run inverts: the receiver recovers the original RTP packet.
    let mut rx = SrtpUnprotector::new(keys.clone());
    assert_eq!(rx.unprotect(&wire).unwrap(), packet.raw);

    // Boundaries on the same run: a short datagram and a flipped tag bit are
    // refusals, not panics.
    let mut short = SrtpUnprotector::new(keys.clone());
    assert_eq!(
        short.unprotect(&wire[..20]),
        Err(ProtectError::TooShort),
        "header + tag is the floor"
    );
    let mut corrupted = wire.clone();
    let last = corrupted.len() - 1;
    corrupted[last] ^= 0x01;
    let mut fresh = SrtpUnprotector::new(keys);
    assert_eq!(fresh.unprotect(&corrupted), Err(ProtectError::Tag));
}

#[test]
fn rfc3711_the_index_carries_the_roc_into_both_iv_and_tag() {
    // Appendix A: the ROC increments on a sequence wrap, and §4.1.1/§4.2.1
    // both consume the resulting 48-bit index. Drive a real wrap ...
    let keys = derive_session_keys(&rfc_master_key(), &rfc_master_salt());
    let mut tx = SrtpProtector::new(keys.clone());
    let last_of_roc0 = tx.protect(&example_packet(0xFFFF));
    let first_of_roc1 = tx.protect(&example_packet(0x0000));

    // ... and check both halves of the claim against the test-side run with
    // the RFC's index for each packet. ROC 1 means index 0x0001_0000, so the
    // IV octets 8..14 differ from the first packet's and the tag covers
    // `00000001` instead of `00000000`.
    assert_eq!(
        hex(&last_of_roc0),
        hex(&rfc_protection_run(&example_packet(0xFFFF), &keys, 0))
    );
    assert_eq!(
        hex(&first_of_roc1),
        hex(&rfc_protection_run(&example_packet(0x0000), &keys, 1))
    );

    // Negative control: the same packet framed with ROC 0 instead of ROC 1
    // must not be what the protector emitted — otherwise the ROC would not be
    // bound into the tag at all, and a replayed post-wrap packet would verify.
    assert_ne!(
        hex(&first_of_roc1),
        hex(&rfc_protection_run(&example_packet(0x0000), &keys, 0))
    );

    // Identical payloads at two different indices must not produce the same
    // keystream: §4.1.1 exists precisely to prevent that reuse. Both packets
    // here are after the wrap, so both carry ROC 1 in the IV and the tag.
    let a = tx.protect(&example_packet(0x0100));
    let b = tx.protect(&example_packet(0x0101));
    assert_ne!(
        a[12..a.len() - AUTH_TAG_LEN],
        b[12..b.len() - AUTH_TAG_LEN],
        "the per-packet index must change the keystream"
    );
    assert_eq!(
        hex(&a),
        hex(&rfc_protection_run(&example_packet(0x0100), &keys, 1))
    );
    assert_eq!(
        hex(&b),
        hex(&rfc_protection_run(&example_packet(0x0101), &keys, 1))
    );
}
