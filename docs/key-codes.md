# Key codes

rbp names every panel control with a 16-bit key code. The table comes from
`ui::KeyInput::getName()` in rbp, which walks the group bits in a fixed order (0x100, 0x200,
0x400, 0x800, 0x2000, 0x1000, 0x8000), so each code is listed under the first group that names it.

Other things rbp attaches to a code:

- **Long press (ms)**: the hold time before a long press, from `getLongPressMs()`.
- **Repeat**: the key repeats while held, from `isRepeatKey()`.
- **Switch**: a latching switch rather than a momentary key, from `isSwitch()`.

Each key event also carries the deck number and a 4-bit index (what happened).
For example, on RotarySelector 0x4205 the index is 1 for push and 2 or 3 for a turn; on Back 0x4206 it is 1 for push and 2 for a long press.

Other group masks that rbp tests:

- 0x3c00: mixer
- 0x4000: deck
- 0x80: meaning unknown

| Code | Name | Group | Long press (ms) | Repeat | Switch |
|---|---|---|---|---|---|
| `0x0201` | Media PC | browse |  |  |  |
| `0x0202` | Media RB | browse |  |  |  |
| `0x0203` | Media USB | browse |  |  |  |
| `0x0204` | Info | browse | 500 |  |  |
| `0x0208` | Browse | browse |  |  |  |
| `0x0209` | TagList | browse |  |  |  |
| `0x020a` | Menu | browse | 1000 |  |  |
| `0x0401` | USB REC | master / effects |  |  |  |
| `0x0409` | TrackMark | master / effects |  |  |  |
| `0x0803` | MicSW | mic |  |  | yes |
| `0x0804` | MicEqHiKnob | mic |  |  |  |
| `0x0805` | MicEqLowKnob | mic |  |  |  |
| `0x1013` | LinePhonoSW | channel |  |  | yes |
| `0x4101` | Play/Pause | deck button |  |  |  |
| `0x4102` | CUE | deck button |  |  |  |
| `0x4103` | Shift | deck button |  |  |  |
| `0x4104` | Vinyl | deck button |  |  |  |
| `0x4108` | TempoRange | deck button |  |  |  |
| `0x4109` | MasterTempo | deck button |  |  |  |
| `0x410a` | TempoSlider | deck button |  |  |  |
| `0x410b` | Sync | deck button | 1000 |  |  |
| `0x410d` | LoopIn | deck button | 700 | yes |  |
| `0x410f` | LoopOut | deck button |  |  |  |
| `0x4110` | Reloop | deck button |  |  |  |
| `0x4111` | Reverse | deck button |  |  |  |
| `0x4112` | Master | deck button |  |  |  |
| `0x4113` | HotCue | deck button |  |  |  |
| `0x4114` | AutoBeatLoop | deck button |  |  |  |
| `0x4115` | LoopSplit | deck button |  |  |  |
| `0x4116` | Slip | deck button |  |  |  |
| `0x4117` | Pad1 | deck button |  |  |  |
| `0x4118` | Pad2 | deck button |  |  |  |
| `0x4119` | Pad3 | deck button |  |  |  |
| `0x411a` | Pad4 | deck button |  |  |  |
| `0x411b` | SearchFwd | deck button | 330 | yes |  |
| `0x411c` | SearchRev | deck button | 330 | yes |  |
| `0x411d` | TouchReleaseKnob | deck button |  |  |  |
| `0x4120` | CueDelete | deck button |  |  |  |
| `0x4121` | CueMemory | deck button |  |  |  |
| `0x4205` | RotarySelector | browse | 500 |  |  |
| `0x4206` | Back | browse | 500 |  |  |
| `0x420b` | TagTrack | browse | 500 |  |  |
| `0x420c` | DeckSelect | browse |  |  |  |
| `0x420d` | Load | browse |  |  |  |
| `0x421b` | TrackFwd | browse |  |  |  |
| `0x421c` | TrackRev | browse |  |  |  |
| `0x4306` | JogWheel | deck button |  |  |  |
| `0x430c` | TimeMode/ACue | deck button | 1000 | yes |  |
| `0x430e` | JogTouch | deck button |  |  |  |
| `0x431e` | CallNext | browse |  |  |  |
| `0x431f` | CallPrev | browse |  |  |  |
| `0x4402` | HeadphoneMixKnob | master / effects |  |  |  |
| `0x4403` | HeadphoneLvKnob | master / effects |  |  |  |
| `0x4404` | LvMasterSelect | master / effects |  |  |  |
| `0x4405` | MasterLvKnob | master / effects |  |  |  |
| `0x440c` | BoothLvKnob | master / effects |  |  |  |
| `0x4486` | Tap | master / effects | 1000 | yes |  |
| `0x4487` | EffectOnOff | master / effects |  |  |  |
| `0x4488` | Quantize | master / effects | 1000 |  |  |
| `0x448a` | TimeKnob | master / effects |  |  |  |
| `0x448b` | LvDepthKnob | master / effects |  |  |  |
| `0x448d` | BeatEffectSW | master / effects |  |  | yes |
| `0x448e` | BfxChSW | master / effects |  |  | yes |
| `0x4493` | BeatPrev | master / effects |  |  |  |
| `0x4494` | BeatNext | master / effects |  |  |  |
| `0x5007` | TrimKnob | channel |  |  |  |
| `0x5008` | IsolatorH | channel |  |  |  |
| `0x5009` | IsolatorM | channel |  |  |  |
| `0x500a` | IsolatorL | channel |  |  |  |
| `0x5011` | ChFader | channel |  |  |  |
| `0x5014` | HeadphoneCueCh | channel |  |  |  |
| `0x508b` | ColorKnob | channel |  |  |  |
| `0x5095` | CfxFilter | channel |  |  |  |
| `0x5096` | CfxCrush | channel |  |  |  |
| `0x5097` | CfxGate | channel |  |  |  |
| `0x5098` | CfxNoise | channel |  |  |  |
| `0x5112` | InputSourceSW | channel |  |  | yes |
| `0x600f` | CrossFader | cross fader |  |  |  |
| `0x6010` | FaderCurveSW | cross fader |  |  | yes |
| `0x8001` | Power | system | 1000 |  |  |
| `0x8002` | UsbStop | system | 1500 |  |  |
