//! MCP server: drive the emulator from an AI agent or a test harness.
//!
//! Transport: MCP Streamable HTTP (JSON-RPC 2.0, JSON responses, stateless) at
//! `POST http://127.0.0.1:<panel port>/mcp` -- served by the panel server, so it is up
//! whenever the app (or `xdj-host`) runs. Claude Code:
//!   `claude mcp add --transport http xdj-rx http://127.0.0.1:8765/mcp`
//!
//! Every tool goes through the panel's own routes (`panel::get` / `panel::post`), so an
//! automated press is exactly a click on the virtual panel: same state model, same
//! snapshots to QEMU, same logs. Control names come from the verified frame map
//! (docs/subucom-frame-map.md); raw frame lines (`B 13 40`, `A 2c 3ff`) work everywhere too.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::{Body, Bytes};
use axum::http::{header, HeaderValue, Method, Response, StatusCode};
use serde_json::{json, Value};

use crate::panel::{self, Panel};
use crate::PowerCmd;

const PROTOCOL: &str = "2025-06-18";

// ---- control names -> frame specs ------------------------------------------------------

enum Ctl {
    Button(u8, u8), // frame offset, bit mask (held while pressed)
    Gpio(u32),      // MAIN-UCOM key GPIO (held LOW while pressed)
    Analog(u8),     // 16-bit value 0..1023 at offset
    Tempo(u8, u8),  // complementary pair: value at .0, 1023-value at .1
}

/// The name table (deck 2 = deck 1 + 0x10; mixer ch 2 = ch 1 + 0x10).
fn controls() -> Vec<(String, Ctl)> {
    let mut v: Vec<(String, Ctl)> = Vec::new();
    let deck_buttons: [(&str, u8, u8); 32] = [
        ("pad_d", 0x10, 0x10), ("pad_c", 0x10, 0x20), ("pad_b", 0x10, 0x40), ("pad_a", 0x10, 0x80),
        ("jog_touch", 0x10, 0x01),
        ("slip", 0x11, 0x01), ("loop_slice", 0x11, 0x02), ("beat_loop", 0x11, 0x04), ("hot_cue", 0x11, 0x08),
        ("search_fwd", 0x11, 0x10), ("search_rev", 0x11, 0x20), ("track_fwd", 0x11, 0x40), ("track_rev", 0x11, 0x80),
        ("shift", 0x12, 0x01), ("rev", 0x12, 0x02), ("master_tempo", 0x12, 0x04), ("tempo_range", 0x12, 0x08),
        ("master", 0x12, 0x10), ("sync", 0x12, 0x20), ("vinyl", 0x12, 0x40), ("reloop", 0x12, 0x80),
        ("loop_out", 0x13, 0x01), ("loop_in", 0x13, 0x02), ("memory", 0x13, 0x04), ("delete", 0x13, 0x08),
        ("call_fwd", 0x13, 0x10), ("call_rev", 0x13, 0x20), ("play", 0x13, 0x40), ("cue", 0x13, 0x80),
        ("", 0, 0), ("", 0, 0), ("", 0, 0),
    ];
    for d in 0..2u8 {
        for (n, off, m) in deck_buttons.iter().filter(|b| !b.0.is_empty()) {
            v.push((format!("deck{}.{n}", d + 1), Ctl::Button(off + d * 0x10, *m)));
        }
        v.push((format!("deck{}.tempo", d + 1), Ctl::Tempo(0x04 + d * 0x10, 0x0a + d * 0x10)));
        v.push((format!("deck{}.vinyl_speed", d + 1), Ctl::Analog(0x06 + d * 0x10)));
    }
    for c in 0..2u8 {
        for (n, off) in [("eq_hi", 0x24u8), ("trim", 0x26), ("eq_low", 0x28), ("eq_mid", 0x2a), ("fader", 0x2c), ("color", 0x2e)] {
            v.push((format!("ch{}.{n}", c + 1), Ctl::Analog(off + c * 0x10)));
        }
    }
    for (n, off) in [("booth", 0x48u8), ("master_level", 0x4a), ("fx_depth", 0x4c), ("crossfader", 0x4e), ("hp_level", 0x50), ("hp_mix", 0x52)] {
        v.push((n.into(), Ctl::Analog(off)));
    }
    for (n, off, m) in [
        ("filter", 0x59u8, 0x10u8), ("crush", 0x59, 0x20), ("gate", 0x59, 0x40), ("noise", 0x59, 0x80),
        ("beat_fwd", 0x5a, 0x01), ("beat_rev", 0x5a, 0x02),
        ("cue_ch2", 0x5b, 0x01), ("cue_ch1", 0x5b, 0x02), ("fx_on", 0x5b, 0x04), ("tap", 0x5b, 0x08),
        ("browse_push", 0x5d, 0x10), ("load_deck2", 0x5d, 0x20), ("load_deck1", 0x5d, 0x40), ("tag_track", 0x5d, 0x80),
        ("back", 0x5e, 0x01), ("track_mark", 0x5e, 0x02), ("master_rec", 0x5e, 0x04), ("usb2_stop", 0x5e, 0x08),
        ("usb1_stop", 0x5e, 0x10), ("quantize", 0x5e, 0x20),
        ("menu", 0x5f, 0x01), ("info", 0x5f, 0x02), ("tag_list", 0x5f, 0x04), ("browse", 0x5f, 0x08),
    ] {
        v.push((n.into(), Ctl::Button(off, m)));
    }
    for (n, g) in [("rekordbox", 159u32), ("midi", 161), ("usb1", 160), ("usb2", 158), ("deck_select", 149), ("time_mode", 146)] {
        v.push((n.into(), Ctl::Gpio(g)));
    }
    v
}

fn lookup(name: &str) -> Option<Ctl> {
    controls().into_iter().find(|(n, _)| n == name).map(|(_, c)| c)
}

// ---- helpers ---------------------------------------------------------------------------

async fn body_text(r: Response<Body>) -> (u16, String) {
    let code = r.status().as_u16();
    let b = axum::body::to_bytes(r.into_body(), 4 << 20).await.unwrap_or_default();
    (code, String::from_utf8_lossy(&b).into_owned())
}

async fn post(p: &Arc<Panel>, path: &str, body: &str) -> Result<String, String> {
    let (code, t) = body_text(panel::post(p.clone(), path, body.to_string()).await).await;
    if code < 300 { Ok(t) } else { Err(format!("{path}: {code} {t}")) }
}

async fn get(p: &Arc<Panel>, path: &str, query: &str) -> Result<String, String> {
    let (code, t) = body_text(panel::get(p.clone(), path, query).await).await;
    if code < 300 { Ok(t) } else { Err(format!("{path}: {code} {t}")) }
}

fn text(s: impl Into<String>) -> Value {
    json!({ "content": [{ "type": "text", "text": s.into() }] })
}

fn b64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut o = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        o.push(T[(n >> 18) as usize & 63] as char);
        o.push(T[(n >> 12) as usize & 63] as char);
        o.push(if c.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        o.push(if c.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    o
}

/// RGB24 -> PNG (zlib-compressed scanlines, filter 0).
fn png(w: u32, h: u32, rgb: &[u8]) -> Vec<u8> {
    use std::io::Write;
    fn chunk(out: &mut Vec<u8>, ty: &[u8; 4], data: &[u8]) {
        out.extend((data.len() as u32).to_be_bytes());
        let mut c = ty.to_vec();
        c.extend_from_slice(data);
        out.extend(&c);
        out.extend(crc32fast::hash(&c).to_be_bytes());
    }
    let mut raw = Vec::with_capacity((w as usize * 3 + 1) * h as usize);
    for row in rgb.chunks(w as usize * 3).take(h as usize) {
        raw.push(0);
        raw.extend_from_slice(row);
    }
    let mut z = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::fast());
    let _ = z.write_all(&raw);
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    let mut ihdr = Vec::new();
    ihdr.extend(w.to_be_bytes());
    ihdr.extend(h.to_be_bytes());
    ihdr.extend([8, 2, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ihdr);
    chunk(&mut out, b"IDAT", &z.finish().unwrap_or_default());
    chunk(&mut out, b"IEND", &[]);
    out
}

fn parse_ppm(d: &[u8]) -> Option<(u32, u32, &[u8])> {
    if !d.starts_with(b"P6") {
        return None;
    }
    let mut i = 2;
    let mut vals = Vec::new();
    while vals.len() < 3 {
        while d.get(i)?.is_ascii_whitespace() {
            i += 1;
        }
        let j = i + d[i..].iter().position(|c| c.is_ascii_whitespace())?;
        vals.push(std::str::from_utf8(&d[i..j]).ok()?.parse::<u32>().ok()?);
        i = j;
    }
    let (w, h) = (vals[0], vals[1]);
    d.get(i + 1..i + 1 + (w * h * 3) as usize).map(|px| (w, h, px))
}

fn console_text(p: &Panel) -> String {
    std::fs::read(&p.cfg.console).map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default()
}

fn s_arg<'a>(a: &'a Value, k: &str) -> Result<&'a str, String> {
    a.get(k).and_then(Value::as_str).ok_or_else(|| format!("missing string argument '{k}'"))
}

// ---- tools -----------------------------------------------------------------------------

fn tool_list() -> Value {
    let t = |name: &str, desc: &str, props: Value, req: &[&str]| {
        json!({ "name": name, "description": desc,
                "inputSchema": { "type": "object", "properties": props, "required": req } })
    };
    json!({ "tools": [
        t("status", "Power state, active firmware, installed release, pending USB, LED payload.", json!({}), &[]),
        t("power", "Power the unit: on, off (the unit's standby handshake while the firmware runs), on_update (firmware update mode), on_test (service test mode).",
          json!({"action": {"type": "string", "enum": ["on", "off", "on_update", "on_test"]}}), &["action"]),
        t("load_firmware", "Prepare a .UPD or Pioneer .zip and boot it as the active firmware. A unit that has never run this firmware provisions itself first (Pioneer's own ALL update, ~1 min), then boots. Becomes the app's firmware on next launch too.",
          json!({"path": {"type": "string", "description": "absolute path to the .UPD/.zip"}}), &["path"]),
        t("install_firmware_update", "Install a .UPD on the current unit like a real update: builds a USB stick from it, boots update mode, inserts it on USB1. The updater ends on 'Turn the power off/on' -- power off and on afterwards.",
          json!({"path": {"type": "string"}}), &["path"]),
        t("list_controls", "Every control name accepted by press/set (buttons, faders/knobs, top-panel keys).", json!({}), &[]),
        t("press", "Press a button: a name from list_controls (e.g. deck1.play, menu, master_rec, rekordbox) or a raw frame line 'B <off> <mask>'. Hold MENU ~2000 ms to open UTILITY.",
          json!({"control": {"type": "string"}, "hold_ms": {"type": "integer", "default": 160}}), &["control"]),
        t("set", "Set a fader/knob (name from list_controls, e.g. ch1.fader, crossfader, deck1.tempo) to 0..1023 (512 = centre).",
          json!({"control": {"type": "string"}, "value": {"type": "integer", "minimum": 0, "maximum": 1023}}), &["control", "value"]),
        t("send_raw", "Send raw panel frame lines (one per line): 'A <off> <hex>' analog, 'B <off> <mask>' hold, 'b'/'a' one-shot, 'S <off> <mask> <bits>' switch field. See docs/subucom-frame-map.md.",
          json!({"lines": {"type": "string"}}), &["lines"]),
        t("release", "Release held buttons: the given 'B <off> <mask>' lines, or everything if empty.", json!({"lines": {"type": "string", "default": ""}}), &[]),
        t("browse", "Turn the browse encoder by N detents (+ = down/forward).", json!({"steps": {"type": "integer"}}), &["steps"]),
        t("jog", "Turn a jog wheel by `degrees` over `seconds` (+ = clockwise), or set touch.",
          json!({"deck": {"type": "integer", "enum": [1, 2]}, "degrees": {"type": "number"}, "seconds": {"type": "number", "default": 0.5}, "touch": {"type": "boolean"}}), &["deck"]),
        t("input_select", "Channel input source.", json!({"channel": {"type": "integer", "enum": [1, 2]}, "source": {"type": "string", "enum": ["deck", "line"]}}), &["channel", "source"]),
        t("insert_usb", "Load a disk image (or /dev/diskN) into USB1/USB2 (read-only snapshot). The browser shows USB1; MASTER REC records to USB2. Queued if the unit is off.",
          json!({"slot": {"type": "string", "enum": ["usb1", "usb2"]}, "path": {"type": "string"}}), &["slot", "path"]),
        t("screenshot", "The unit's 800x480 screen as a PNG image.", json!({}), &[]),
        t("leds", "rbp's current LED/meter payload (hex; layout in docs/subucom-led-map.md).", json!({}), &[]),
        t("console", "The guest Linux console of the current power-on: the last `tail` lines, optionally only those containing `grep`.",
          json!({"tail": {"type": "integer", "default": 40}, "grep": {"type": "string"}}), &[]),
        t("wait_for_console", "Wait until the console contains `marker` (e.g. 'Resolve xrun' = firmware fully up). Returns the seconds waited.",
          json!({"marker": {"type": "string"}, "timeout_s": {"type": "integer", "default": 180}}), &["marker"]),
    ]})
}

async fn call(p: &Arc<Panel>, name: &str, a: &Value) -> Result<Value, String> {
    match name {
        "status" => {
            let pw: Value = serde_json::from_str(&get(p, "/power", "").await?).unwrap_or(Value::Null);
            let fw = p.fw_dir();
            let env = xdj_fw::nand::env(&fw.join("nand-flash.img")).ok().flatten();
            let release = env.and_then(|e| e.into_iter().rev().find(|(k, _)| k == "release").map(|(_, v)| v));
            let leds: Value = serde_json::from_str(&get(p, "/leds", "once=1").await?).unwrap_or(Value::Null);
            Ok(text(serde_json::to_string_pretty(&json!({
                "power": pw, "firmware_dir": fw, "installed_release": release,
                "rbp_running": console_text(p).contains("commandline read: rbp"), "leds": leds["hex"],
            })).unwrap()))
        }
        "power" => {
            let body = match s_arg(a, "action")? {
                "on" => "on", "off" => "off", "on_update" => "on update", "on_test" => "on test",
                x => return Err(format!("unknown action {x}")),
            };
            Ok(text(post(p, "/power", body).await?))
        }
        "load_firmware" => {
            let path = PathBuf::from(s_arg(a, "path")?);
            if !path.is_file() {
                return Err(format!("no such file: {}", path.display()));
            }
            let r = p.power_cmd(PowerCmd::LoadFirmware(path.clone())).await;
            if r.starts_with("error") {
                return Err(r);
            }
            Ok(text(format!("{} is the active firmware; power: {r}{}", path.display(),
                if r == "on update" { " (provisioning a blank unit with the firmware's ALL update, then it boots)" } else { "" })))
        }
        "install_firmware_update" => Ok(text(post(p, "/install-update", s_arg(a, "path")?).await?)),
        "list_controls" => {
            let mut out = String::from("name\tkind\tframe\n");
            for (n, c) in controls() {
                let (k, f) = match c {
                    Ctl::Button(o, m) => ("button", format!("B {o:02x} {m:02x}")),
                    Ctl::Gpio(g) => ("top-panel key", format!("gpio {g}")),
                    Ctl::Analog(o) => ("0..1023", format!("A {o:02x}")),
                    Ctl::Tempo(x, y) => ("0..1023", format!("A {x:02x} v + A {y:02x} 1023-v")),
                };
                out.push_str(&format!("{n}\t{k}\t{f}\n"));
            }
            Ok(text(out))
        }
        "press" => {
            let c = s_arg(a, "control")?;
            let hold = Duration::from_millis(a.get("hold_ms").and_then(Value::as_u64).unwrap_or(160));
            match lookup(c) {
                Some(Ctl::Gpio(g)) => {
                    post(p, "/mainkey", &g.to_string()).await?;
                    tokio::time::sleep(hold).await;
                    post(p, "/mainkey_release", "").await?;
                }
                Some(Ctl::Button(o, m)) => {
                    let spec = format!("B {o:02x} {m:02x}");
                    post(p, "/ctrl", &spec).await?;
                    tokio::time::sleep(hold).await;
                    post(p, "/release", &spec).await?;
                }
                Some(_) => return Err(format!("{c} is a fader/knob: use set")),
                None if c.trim_start().starts_with('B') => {
                    post(p, "/ctrl", c).await?;
                    tokio::time::sleep(hold).await;
                    post(p, "/release", c).await?;
                }
                None => return Err(format!("unknown control {c} (see list_controls)")),
            }
            Ok(text(format!("pressed {c} for {} ms", hold.as_millis())))
        }
        "set" => {
            let c = s_arg(a, "control")?;
            let v = a.get("value").and_then(Value::as_i64).ok_or("missing integer 'value'")?.clamp(0, 1023);
            let lines = match lookup(c) {
                Some(Ctl::Analog(o)) => format!("A {o:02x} {v:x}"),
                Some(Ctl::Tempo(x, y)) => format!("A {x:02x} {v:x}\nA {y:02x} {:x}", 1023 - v),
                _ => return Err(format!("{c} is not a fader/knob (see list_controls)")),
            };
            post(p, "/ctrl", &lines).await?;
            Ok(text(format!("{c} = {v}")))
        }
        "send_raw" => Ok(text(post(p, "/ctrl", s_arg(a, "lines")?).await?)),
        "release" => Ok(text(post(p, "/release", a.get("lines").and_then(Value::as_str).unwrap_or("")).await?)),
        "browse" => {
            let n = a.get("steps").and_then(Value::as_i64).ok_or("missing integer 'steps'")?;
            post(p, "/browse", &n.to_string()).await?;
            tokio::time::sleep(Duration::from_millis(45 * n.unsigned_abs().min(500) + 100)).await;
            Ok(text(format!("browse {n:+}")))
        }
        "jog" => {
            let d = a.get("deck").and_then(Value::as_i64).ok_or("missing 'deck'")?;
            if let Some(t) = a.get("touch").and_then(Value::as_bool) {
                post(p, "/jog", &format!("{d} touch {}", t as u8)).await?;
            }
            if let Some(deg) = a.get("degrees").and_then(Value::as_f64) {
                let sec = a.get("seconds").and_then(Value::as_f64).unwrap_or(0.5);
                post(p, "/jog", &format!("{d} move {deg} {sec}")).await?;
            }
            Ok(text("ok"))
        }
        "input_select" => {
            let ch = a.get("channel").and_then(Value::as_i64).ok_or("missing 'channel'")?;
            let src = s_arg(a, "source")?.to_uppercase();
            Ok(text(post(p, "/input", &format!("{} {src}", ch - 1)).await?))
        }
        "insert_usb" => Ok(text(post(p, "/usb", &format!("{} {}", s_arg(a, "slot")?, s_arg(a, "path")?)).await?)),
        "leds" => Ok(text(get(p, "/leds", "once=1").await?)),
        "console" => {
            let tail = a.get("tail").and_then(Value::as_u64).unwrap_or(40) as usize;
            let g = a.get("grep").and_then(Value::as_str);
            let con = console_text(p);
            let lines: Vec<&str> = con.lines().filter(|l| g.is_none_or(|g| l.contains(g))).collect();
            Ok(text(lines[lines.len().saturating_sub(tail)..].join("\n")))
        }
        "wait_for_console" => {
            let m = s_arg(a, "marker")?;
            let to = Duration::from_secs(a.get("timeout_s").and_then(Value::as_u64).unwrap_or(180));
            let t0 = Instant::now();
            while t0.elapsed() < to {
                if console_text(p).contains(m) {
                    return Ok(text(format!("found after {:.1} s", t0.elapsed().as_secs_f64())));
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
            Err(format!("'{m}' not on the console after {} s", to.as_secs()))
        }
        "screenshot" => {
            let tmp = std::env::temp_dir().join(format!("xdj-mcp-{}.ppm", std::process::id()));
            let _ = std::fs::remove_file(&tmp);
            crate::qmp_hmp(&p.cfg.qmp_sock, &[format!("screendump {}", tmp.display())]).await?;
            let t0 = Instant::now();
            let mut data = Vec::new();
            while t0.elapsed() < Duration::from_secs(3) {
                if let Ok(d) = std::fs::read(&tmp) {
                    if parse_ppm(&d).is_some() {
                        data = d;
                        break;
                    }
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            let _ = std::fs::remove_file(&tmp);
            let (w, h, px) = parse_ppm(&data).ok_or("no screen (is the unit powered on?)")?;
            Ok(json!({ "content": [{ "type": "image", "mimeType": "image/png", "data": b64(&png(w, h, px)) }] }))
        }
        _ => Err(format!("unknown tool {name}")),
    }
}

// ---- JSON-RPC over HTTP -----------------------------------------------------------------

fn reply(v: &Value) -> Response<Body> {
    let mut r = Response::new(Body::from(v.to_string()));
    r.headers_mut().insert(header::CONTENT_TYPE, HeaderValue::from_static("application/json"));
    r
}

async fn one(p: &Arc<Panel>, req: &Value) -> Option<Value> {
    let id = req.get("id").cloned();
    let method = req.get("method").and_then(Value::as_str).unwrap_or("");
    let params = req.get("params").cloned().unwrap_or(Value::Null);
    let result: Result<Value, (i64, String)> = match method {
        "initialize" => Ok(json!({
            "protocolVersion": params.get("protocolVersion").and_then(Value::as_str).unwrap_or(PROTOCOL),
            "capabilities": { "tools": { "listChanged": false } },
            "serverInfo": { "name": "xdj-rx", "title": "XDJ-RX emulator", "version": env!("CARGO_PKG_VERSION") },
            "instructions": "Drives the emulated Pioneer XDJ-RX: power, firmware, every panel control, USB, screen, LEDs and console. \
                Typical flow: status -> power on -> wait_for_console 'Resolve xrun' (firmware up) -> press/set/browse -> screenshot. \
                Hold MENU (press menu hold_ms 2000) for UTILITY; the browser shows USB1."
        })),
        "ping" => Ok(json!({})),
        "tools/list" => Ok(tool_list()),
        "tools/call" => {
            let name = params.get("name").and_then(Value::as_str).unwrap_or("");
            let args = params.get("arguments").cloned().unwrap_or(json!({}));
            Ok(match call(p, name, &args).await {
                Ok(v) => v,
                Err(e) => json!({ "content": [{ "type": "text", "text": e }], "isError": true }),
            })
        }
        m if m.starts_with("notifications/") => return None,
        m => Err((-32601, format!("method not found: {m}"))),
    };
    let id = id?; // a request without an id is a notification: no response
    Some(match result {
        Ok(r) => json!({ "jsonrpc": "2.0", "id": id, "result": r }),
        Err((c, m)) => json!({ "jsonrpc": "2.0", "id": id, "error": { "code": c, "message": m } }),
    })
}

/// `/mcp`: POST = JSON-RPC (single or batch); GET (server-initiated stream) is not offered.
pub async fn handle(p: Arc<Panel>, method: Method, body: Bytes) -> Response<Body> {
    if method != Method::POST {
        let mut r = Response::new(Body::from("MCP: POST JSON-RPC to this endpoint"));
        *r.status_mut() = StatusCode::METHOD_NOT_ALLOWED;
        return r;
    }
    let req: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => return reply(&json!({ "jsonrpc": "2.0", "id": null, "error": { "code": -32700, "message": format!("parse error: {e}") } })),
    };
    let out = if let Some(batch) = req.as_array() {
        let mut v = Vec::new();
        for r in batch {
            if let Some(x) = one(&p, r).await {
                v.push(x);
            }
        }
        if v.is_empty() { None } else { Some(Value::Array(v)) }
    } else {
        one(&p, &req).await
    };
    match out {
        Some(v) => reply(&v),
        None => {
            let mut r = Response::new(Body::empty());
            *r.status_mut() = StatusCode::ACCEPTED;
            r
        }
    }
}

