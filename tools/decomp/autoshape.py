"""Generate small functions from a table of instruction shapes and C templates.

Each ungrafted function up to 128 bytes (SHAPE_MAX) is reduced to a shape: its instructions with every
immediate replaced by N and every symbol by X (N0, N1, … and X0, X1, … in order of appearance).
A table maps shapes to one or more C templates. Template holes:
  {N0}      the first immediate (as written, decimal or hex)
  {F0}      a callee named after symbol X0: `extern "C" int callee(...)` with X0 as assembler name
  {L0}      the same callee returning long long ({B0}: returning bool)
  {D0}      a data object named after symbol X0: `extern "C" int data[]`
  Literal-pool loads appear as `ldr rX, =X` with the pool word's symbol as capture.
  {A0}      the address symbol X0 stands for, as a number (pool constants that are offsets)
  {V0}      the first immediate divided by 4 (a vtable slot or int index)
Shapes with literal-pool loads are also tried as cold functions (GCC optimises those for size
and loads addresses from the pool instead of movw/movt). Every template of a shape is compiled; the first one matching byte for byte is kept, so a table
entry can list guesses. Writes src/auto/auto_shape.cpp and obj/auto_shape.pairs. Run from decomp/.
"""
import os
import re
import subprocess
import sys

FLAGS = ('-O2 -mcpu=cortex-a8 -marm -mfpu=neon -mfloat-abi=softfp ' + __import__('os').environ.get('EXTRA_FLAGS', '')).split()
OUT = __import__('os').environ.get('AUTOSHAPE_OUT', 'src/auto/auto_shape.cpp')
P = 'char* p'
A4 = 'int a, int b, int c, int d'
VT = ('struct VO { ' + ' '.join(f'virtual long long v{k}(int, int);' for k in range(64)) + ' };\n'
      'struct VB { ' + ' '.join(f'virtual bool v{k}(int, int);' for k in range(64)) + ' };\n'
      'struct VN { ' + ' '.join(f'virtual long long v{k}();' for k in range(64)) + ' };\n'
      'struct P2 { int a, b; };\n'
      'struct VV { ' + ' '.join(f'virtual void v{k}();' for k in range(64)) + ' };\n'
      'struct VP { ' + ' '.join(f'virtual VP* v{k}(int);' for k in range(64)) + ' };\n'
      'struct VI { ' + ' '.join(f'virtual int v{k}(int);' for k in range(64)) + ' };\n'
      'struct VI3 { ' + ' '.join(f'virtual int v{k}(int, int, int);' for k in range(64)) + ' };\n'
      'struct V3 { ' + ' '.join(f'virtual long long v{k}(int, int, int);' for k in range(64)) + ' };\n'
      'typedef long long (*FnL)();\n'
      'typedef long long (*FnL3)(void*, int, int);\n'
      'typedef long long U64A4 __attribute__ ((aligned (4)));')

# shape -> [(signature, body), ...]; names are placeholders.
TABLE = {
    'push {r4, r5, lr} ; sub sp, sp, #N ; mov r5, r0 ; mov r4, r1 ; mov r2, #N ; add r0, sp, #N ; mov r1, #N ; bl X ; add r0, sp, #N ; str r5, [sp, #N] ; movw r3, #:lower16:X ; str r4, [sp, #N] ; movt r3, #:upper16:X ; str r3, [sp, #N] ; bl X ; add sp, sp, #N ; pop {r4, r5, pc}': [
        ('void f(int a, int b)', 'struct M {{ int w[{N1} / 4]; }}; M m; {F0}(&m, 0, sizeof m); m.w[({N5} - {N2}) / 4] = a; m.w[({N6} - {N2}) / 4] = b; m.w[({N7} - {N2}) / 4] = (int) {D1}; {F3}(&m);')],
    'push {r4, lr} ; sub sp, sp, #N ; mov r4, r0 ; mov r1, #N ; mov r2, #N ; add r0, sp, #N ; bl X ; add r0, sp, #N ; str r4, [sp, #N] ; movw r3, #:lower16:X ; movt r3, #:upper16:X ; str r3, [sp, #N] ; bl X ; add sp, sp, #N ; pop {r4, pc}': [
        ('void f(int a)', 'struct M {{ int w[{N2} / 4]; }}; M m; {F0}(&m, 0, sizeof m); m.w[({N5} - {N3}) / 4] = a; m.w[({N6} - {N3}) / 4] = (int) {D1}; {F3}(&m);')],
    'push {r4, lr} ; mov r4, r0 ; bl X ; movw r3, #:lower16:X ; movt r3, #:upper16:X ; str r3, [r4] ; mov r3, #N ; strb r3, [r4, #N] ; pop {r4, pc}': [
        (f'void f({P})', '{F0}(p); *(void**) p = (void*) {D1}; p[{N1}] = {N0};')],
    'push {r4, lr} ; mov r0, #N ; bl X ; subs r4, r0, #N ; beq #N ; mov r1, #N ; mov r2, #N ; bl X ; mov r0, r4 ; bl X ; mov r3, #N ; strb r3, [r4, #N] ; ldr r3, =X ; str r3, [r4] ; mov r0, r4 ; pop {r4, pc}': [
        ('char* f(void)', 'char* p = (char*) {F0}({N0}); if (p) {{ {F1}(p, {N3}, {N4}); {F2}(p); p[{N6}] = {N5}; *(void**) p = (void*) {D3}; }} return p;')],
    'push {r3, lr} ; bl X ; cmp r0, #N ; popeq {r3, pc} ; ldr r3, =X ; mov r2, #N ; mov r0, #N ; ldr r1, =X ; str r2, [r3] ; bl X ; mov r0, #N ; pop {r3, pc}': [
        ('int f(void)', 'int r = {F0}(); if (r == {N0}) return r; {D1}[0] = {N1}; {F3}({N2}, {D2}); return {N3};')],
    'ldr r0, [r0, #N] ; movw r3, #:lower16:X ; movt r3, #:upper16:X ; cmp r0, #N ; moveq r0, r3 ; bx lr': [
        (f'char* f({P})', 'char* q = *(char**) (p + {N0}); return q ? q : (char*) {D0};')],
    'ldr r3, [r0, #N] ; ldr r0, [r3] ; b X': [
        (f'int f({P})', 'return {F0}(**(int**) (p + {N0}));')],
    'ldr r0, [r0, #N] ; cmp r0, #N ; ldrne r3, [r0, #N] ; ldrbne r0, [r3, #N] ; bx lr': [
        (f'int f({P})', 'char* q = *(char**) (p + {N0}); return q ? (*(unsigned char**) (q + {N2}))[{N3}] : 0;')],
    'ldr r3, [r0, #N] ; ldr r0, [r3, #N] ; rsbs r0, r0, #N ; movlo r0, #N ; bx lr': [
        (f'bool f({P})', 'return *(int*) (*(char**) (p + {N0}) + {N1}) == 0;')],
    'ldr r0, [r0, #N] ; cmp r0, #N ; bxeq lr ; add r0, r0, #N ; b X': [
        (f'int f({P})', 'char* q = *(char**) (p + {N0}); if (!q) return 0; return {F0}(q + {N2});')],
    'mov r3, #N ; str r1, [r0, #N] ; strb r3, [r0, #N] ; bx lr': [
        (f'void f({P}, int v)', '*(int*) (p + {N1}) = v; p[{N2}] = {N0};')],
    'mov r3, #N ; str r3, [r0, #N] ; str r3, [r0, #N] ; bx lr': [
        (f'void f({P})', '*(int*) (p + {N1}) = {N0}; *(int*) (p + {N2}) = {N0};')],
    'ldr r0, [r0, #N] ; cmp r0, #N ; bxeq lr ; mov r1, #N ; mov r2, r1 ; b X': [
        (f'int f({P})', 'void* q = *(void**) (p + {N0}); if (!q) return 0; return {F0}(q, {N2}, {N2});')],
    'ldr r3, =X ; add r0, r3, r0, lsl #N ; bx lr': [
        ('void* f(int i)', 'struct S {{ char b[1 << {N0}]; }}; return &((S*) {D0})[i];')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r0, [r3, #N] ; sub r1, r0, #N ; rsbs r0, r1, #N ; adc r0, r0, r1 ; bx lr': [
        ('bool f(void)', 'return {D0}[{V0}] == {N1};')],
    'push {r3, lr} ; bl X ; sub r3, r0, #N ; rsbs r0, r3, #N ; adc r0, r0, r3 ; pop {r3, pc}': [
        (f'bool f({A4})', 'return {F0}(a, b, c, d) == {N0};')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; mov r2, #N ; mla r0, r2, r0, r3 ; bx lr': [
        ('void* f(int i)', 'struct S {{ char b[{N0}]; }}; return &((S*) {D0})[i];')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; mov r2, #N ; mla r3, r2, r0, r3 ; ldrh r0, [r3, #N] ; bx lr': [
        ('int f(int i)', 'struct S {{ char pad[{N1}]; unsigned short x; char pad2[{N0} - {N1} - 2]; }}; return ((S*) {D0})[i].x;')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r0, [r3, #N] ; adds r0, r0, #N ; movne r0, #N ; bx lr': [
        ('bool f(void)', 'return {D0}[{V0}] != 0;')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldrb r0, [r3, #N] ; ubfx r0, r0, #N, #N ; bx lr': [
        ('int f(void)', 'return (((unsigned char*) {D0})[{N0}] >> {N1}) & ((1 << {N2}) - 1);')],
    'mov r1, r0 ; movw r0, #:lower16:X ; movt r0, #:upper16:X ; b X': [
        ('int f(int a)', 'return {F2}({D0}, a);')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; b X': [
        ('int f(int a, int b, int c)', 'return {F2}(a, b, c, {D0});')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; strh r0, [r3, #N] ; bx lr': [
        ('void f(short v)', '*(short*) ((char*) {D0} + {N0}) = v;')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; movw r2, #N ; ldrb r0, [r3, r2] ; bx lr': [
        ('int f(void)', 'return ((unsigned char*) {D0})[{N0}];')],
    'cmp r0, #N ; movhi r0, #N ; movls r0, #N ; bx lr': [
        ('int f(unsigned a)', 'return a > {N0} ? {N1} : {N2};')],
    'sub r0, r0, #N ; cmp r0, #N ; movhi r0, #N ; movls r0, #N ; bx lr': [
        ('int f(int a)', 'return (unsigned) (a - {N0}) > {N1} ? {N2} : {N3};')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; str r0, [r3, #N] ; mov r0, #N ; bx lr': [
        ('int f(int v)', '*(int*) ((char*) {D0} + {N0}) = v; return {N1};')],
    'push {r3, lr} ; add r0, r0, #N ; bl X ; mov r0, #N ; pop {r3, pc}': [
        (f'int f({P})', '{F0}(p + {N0}); return {N1};')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r0, [r3, #N] ; b X': [
        ('int f(int x, int a, int b, int c)', 'return {F2}(*(int*) ((char*) {D0} + {N0}), a, b, c);'),
        ('void f(void)', '{F2}(*(int*) ((char*) {D0} + {N0}));')],
    'ldmib r0, {r0, r1, r2} ; b X': [
        ('int f(int* p)', 'return {F0}(p[1], p[2], p[3]);')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; str r3, [r0] ; bx lr': [
        (f'void f({P})', '*(void**) p = (void*) {D0};')],
    'push {r4, lr} ; mov r4, r0 ; add r0, r0, #N ; bl X ; mov r0, r4 ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        (f'char* f({P})', '{F0}(p + {N0}); {F1}(p); return p;')],
    'push {r4, lr} ; mov r4, r0 ; movw r0, #:lower16:X ; movt r0, #:upper16:X ; bl X ; mov r0, r4 ; movw r1, #:lower16:X ; movt r1, #:upper16:X ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        (f'char* f({P})', '{F2}({D0}); {F5}(p, {D3}); return p;')],
    'push {r4, lr} ; mov r4, r0 ; ldr r0, [r0, #N] ; cmp r0, #N ; beq #N ; bl X ; mov r3, #N ; str r3, [r4, #N] ; mov r0, r4 ; pop {r4, pc}': [
        (f'char* f({P})', 'void* q = *(void**) (p + {N0}); if (q) {F0}(q); *(int*) (p + {N4}) = {N3}; return p;')],
    'push {r4, lr} ; mov r4, r0 ; ldr r0, [r0, #N] ; cmp r0, #N ; beq #N ; bl X ; mov r3, #N ; str r3, [r4, #N] ; str r3, [r4, #N] ; str r3, [r4, #N] ; mov r0, r4 ; pop {r4, pc}': [
        (f'char* f({P})', 'void* q = *(void**) (p + {N0}); if (q) {F0}(q); *(int*) (p + {N4}) = {N3}; *(int*) (p + {N5}) = {N3}; *(int*) (p + {N6}) = {N3}; return p;')],
    'ldrb r3, [r0, #N] ; cmp r3, #N ; bne #N ; mov r0, r3 ; bx lr ; b X': [
        (f'int f({P})', 'if (p[{N0}] == 0) return 0; return {F0}(p);')],
    'push {r3, lr} ; bl X ; pop {r3, lr} ; b X': [
        (f'int f({A4})', 'return {F1}({F0}(a, b, c, d));')],
    'push {r3, lr} ; bl X ; bl X ; bl X ; mov r0, #N ; pop {r3, pc}': [
        ('int f(void)', '{F0}(); {F1}(); {F2}(); return {N0};')],
    'cmp r1, #N ; addls r1, r0, r1 ; movls r3, #N ; strbls r3, [r1, #N] ; bx lr': [
        (f'void f({P}, unsigned i)', 'if (i <= {N0}) p[i + {N2}] = {N1};')],
    'str lr, [sp, #N]! ; sub sp, sp, #N ; mov ip, #N ; str ip, [sp] ; bl X ; add sp, sp, #N ; ldm sp!, {pc}': [
        (f'int f({A4})', 'return {F0}(a, b, c, d, {N2});')],
    'str lr, [sp, #N]! ; sub sp, sp, #N ; mvn ip, #N ; str ip, [sp] ; bl X ; add sp, sp, #N ; ldm sp!, {pc}': [
        (f'int f({A4})', 'return {F0}(a, b, c, d, ~{N2});')],
    'str lr, [sp, #N]! ; sub sp, sp, #N ; mov r0, sp ; bl X ; ldr r0, [sp, #N] ; add sp, sp, #N ; ldm sp!, {pc}': [
        ('int f(void)', 'int buf[({N1} - 4) / 4]; {F0}(buf); return buf[{V2}];')],
    'push {r4, lr} ; mov r3, r0 ; ldr r0, [r0, #N] ; ldr r4, [r3, #N] ; bl X ; str r0, [r4] ; pop {r4, pc}': [
        (f'void f({P})', 'int* out = *(int**) (p + {N1}); *out = {F0}(*(int*) (p + {N0}));')],
    'mov r1, r0 ; ldr r0, [r0, #N] ; cmp r0, #N ; beq #N ; add r1, r1, #N ; b X ; mvn r0, #N ; bx lr': [
        (f'int f({P})', 'void* q = *(void**) (p + {N0}); if (!q) return -1; return {F0}(q, p + {N3});')],
    'mov r3, r0 ; ldr r0, [r0, #N] ; mov r2, r1 ; cmp r0, #N ; bxeq lr ; add r1, r3, #N ; b X': [
        (f'int f({P}, int a)', 'void* q = *(void**) (p + {N0}); if (!q) return 0; return {F0}(q, p + {N2}, a);')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r0, [r3, r0, lsl #N] ; bx lr': [
        ('int f(int i)', 'return {D0}[i];')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; mov r2, #N ; mla r3, r2, r0, r3 ; ldr r0, [r3, #N] ; bx lr': [
        ('int f(int i)', 'struct S {{ char pad[{N1}]; int x; char pad2[{N0} - {N1} - 4]; }}; return ((S*) {D0})[i].x;')],
    'sub r0, r0, #N ; cmp r0, #N ; ldrls r3, =X ; movhi r0, #N ; ldrls r0, [r3, r0, lsl #N] ; bx lr': [
        ('int f(unsigned i)', 'i -= {N0}; if (i > {N1}) return {N2}; return {D0}[i];')],
    'push {r3, lr} ; bl X ; movw r3, #:lower16:X ; movt r3, #:upper16:X ; str r0, [r3] ; pop {r3, pc}': [
        ('void f(void)', '{D1}[0] = {F0}();'),
        ('int f(void)', 'return {D1}[0] = {F0}();')],
    'rsb r0, r1, r0 ; bx lr': [
        ('int f(int a, int b)', 'return a - b;')],
    'push {r3, lr} ; mov r1, #N ; movw r0, #:lower16:X ; movt r0, #:upper16:X ; bl X ; mov r0, #N ; pop {r3, pc}': [
        ('int f(void)', '{F2}({D0}, {N0}); return {N1};')],
    'push {r3, lr} ; bl X ; cmp r0, #N ; mvnne r0, #N ; pop {r3, pc}': [
        (f'int f({A4})', 'if ({F0}(a, b, c, d) != {N0}) return -1; return {N0};')],
    'push {r3, lr} ; bl X ; cmp r0, #N ; mvneq r0, #N ; movne r0, #N ; pop {r3, pc}': [
        (f'int f({A4})', 'if ({F0}(a, b, c, d) == {N0}) return -1; return {N2};')],
    'mov r3, r0 ; ldr r0, [r0, #N] ; ldr r1, [r3, #N] ; b X': [
        (f'int f({P})', 'return {F0}(*(int*) (p + {N0}), *(int*) (p + {N1}));')],
    'mov r3, r0 ; mov r2, #N ; ldr r0, [r0, #N] ; str r2, [r3, #N] ; str r1, [r3, #N] ; bx lr': [
        (f'int f({P}, int v)', 'int old = *(int*) (p + {N1}); *(int*) (p + {N2}) = {N0}; *(int*) (p + {N3}) = v; return old;')],
    'mov r3, r0 ; mov r2, #N ; str r1, [r0, #N] ; mov r0, r2 ; str r2, [r3, #N] ; bx lr': [
        (f'int f({P}, int v)', '*(int*) (p + {N1}) = v; *(int*) (p + {N2}) = {N0}; return {N0};')],
    'ldr r0, [r0, #N] ; rsbs r0, r0, #N ; movlo r0, #N ; bx lr': [
        (f'bool f({P})', 'return *(int*) (p + {N0}) == 0;')],
    'ldr r0, [r0, #N] ; push {r3, lr} ; ldr ip, [r0] ; ldr ip, [ip, #N] ; blx ip ; pop {r3, pc}': [
        (f'int f({P}, int a, int b, int c)', 'return (*(VI3**) (p + {N0}))->v{V1}(a, b, c);')],
    'ldr r0, [r0, #N] ; cmp r0, #N ; ldrbne r0, [r0, #N] ; ubfxne r0, r0, #N, #N ; bx lr': [
        (f'int f({P})', 'unsigned char* q = *(unsigned char**) (p + {N0}); return q ? (q[{N2}] >> {N3}) & ((1 << {N4}) - 1) : 0;')],
    'mov r3, #N ; strb r3, [r0, #N] ; strb r1, [r0, #N] ; bx lr': [
        (f'void f({P}, char v)', 'p[{N1}] = {N0}; p[{N2}] = v;')],
    'ldr r3, [r0, #N] ; ldrb r2, [r1] ; strb r2, [r3] ; bx lr': [
        (f'void f({P}, char* q)', '**(char**) (p + {N0}) = *q;')],
    'mov r3, r1 ; mov r1, r2 ; ldr r2, [r3] ; b X': [
        ('int f(int a, int* b, int c)', 'return {F0}(a, c, *b);')],
    'push {r4, lr} ; mov r4, r0 ; ldr r3, [r0, #N] ; ldr r0, [r0, #N] ; blx r3 ; mov r3, #N ; str r3, [r4, #N] ; pop {r4, pc}': [
        (f'void f({P})', 'typedef void (*Fn) (void*); (*(Fn*) (p + {N0})) (*(void**) (p + {N1})); *(int*) (p + {N3}) = {N2};')],
    'push {r3, lr} ; ldr r3, [r0, #N] ; cmp r3, #N ; beq #N ; ldr r0, [r0, #N] ; blx r3 ; pop {r3, pc} ; mov r0, #N ; pop {r3, pc}': [
        (f'int f({P})', 'typedef int (*Fn) (void*); Fn fn = *(Fn*) (p + {N0}); if (!fn) return {N4}; return fn (*(void**) (p + {N3}));')],
    'ldrb ip, [sp, #N] ; ldr r0, [r0, #N] ; str ip, [sp, #N] ; b X': [
        (f'int f({P}, int a, int b, int c, int e, unsigned char d)', 'return {F0}(*(void**) (p + {N1}), a, b, c, e, d);')],
    'ldrb r3, [r0, #N] ; cmp r3, r1 ; strbne r1, [r0, #N] ; bx lr': [
        (f'void f({P}, unsigned char v)', 'if ((unsigned char) p[{N0}] != v) p[{N1}] = v;')],
    'push {r4, lr} ; mov r4, r0 ; bl X ; mov r1, r4 ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; adds r0, r0, #N ; movne r0, #N ; pop {r4, pc}': [
        ('bool f(int a)', 'return ((VI*) (long) {F0}())->v{V0}(a) != 0;')],
    'ldr r3, [r1] ; cmp r3, #N ; addne r3, r0, r3, lsl #N ; moveq r0, #N ; ldrne r3, [r3, #N] ; ldrne r0, [r3, #N] ; bx lr': [
        (f'int f({P}, int* i)', 'int k = *i; if (k == {N0}) return {N2}; struct S {{ char pad[{N3}]; int* e[1]; }}; return ((S*) p)->e[k][{V4}];')],
    'ldr r3, [r1] ; cmp r3, #N ; addne r3, r0, r3, lsl #N ; moveq r0, #N ; ldrne r3, [r3, #N] ; ldrbne r0, [r3, #N] ; bx lr': [
        (f'unsigned char f({P}, int* i)', 'int k = *i; if (k == {N0}) return {N2}; struct S {{ char pad[{N3}]; unsigned char* e[1]; }}; return ((S*) p)->e[k][{N4}];')],
    'push {r3, lr} ; bl X ; eor r0, r0, #N ; uxtb r0, r0 ; pop {r3, pc}': [
        (f'bool f({A4})', 'return !{B0}(a, b, c, d);')],
    'push {r4, lr} ; subs r4, r1, #N ; popeq {r4, pc} ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; mov r1, r4 ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; pop {r4, pc}': [
        ('void f(VP* o, int b)', 'if (b) o->v{V1}(b)->v{V2}(b);')],
    'ldr r3, =X ; sub r0, r0, #N ; ldr r3, [r3, #N] ; ldr r2, [r3, #N] ; cmp r0, r2 ; ldrlo r3, [r3, #N] ; movhs r0, #N ; ldrlo r0, [r3, r0, lsl #N] ; bx lr': [
        ('int f(int a)', 'char* g = *(char**) ((char*) {D0} + {N1}); unsigned i = a - {N0}; if (i < *(unsigned*) (g + {N2})) return (*(int**) (g + {N3}))[i]; return 0;')],
    'push {r4, lr} ; mov r4, r0 ; ldr r0, [r0, #N] ; bl X ; mov r3, #N ; str r3, [r4, #N] ; str r3, [r4, #N] ; pop {r4, pc}': [
        (f'void f({P})', '{F0}(*(void**) (p + {N0})); *(int*) (p + {N2}) = {N1}; *(int*) (p + {N3}) = {N1};')],
    'ldr ip, =X ; push {r3, lr} ; ldr ip, [ip, #N] ; cmp ip, #N ; popeq {r3, pc} ; blx ip ; pop {r3, pc}': [
        ('void f(int a, int b, int c, int d)', 'typedef void (*FnA) (int, int, int, int); FnA fn = *(FnA*) ((char*) {D0} + {N0}); if (fn) fn (a, b, c, d);'),
        ('int f(int a, int b, int c, int d)', 'typedef int (*FnA) (int, int, int, int); FnA fn = *(FnA*) ((char*) {D0} + {N0}); if (!fn) return 0; return fn (a, b, c, d);')],
    'movw r0, #:lower16:X ; movw r1, #:lower16:X ; movt r0, #:upper16:X ; movt r1, #:upper16:X ; b X': [
        ('int f(void)', 'return {F4}({D0}, {D1});')],
    'sub sp, sp, #N ; add r3, sp, #N ; stmdb r3, {r0, r1} ; mov r0, #N ; add sp, sp, #N ; bx lr': [
        ('int f(P2 s)', 'return {N2};')],
    'ldr r0, [r0, #N] ; cmp r0, #N ; ldrne r0, [r0, r1, lsl #N] ; bx lr': [
        (f'int f({P}, int i)', 'int* a = *(int**) (p + {N0}); return a ? a[i] : 0;')],
    'ldr r0, =X ; b X': [
        ('int f(void)', 'return {F1}({D0});')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; mov r2, #N ; str r2, [r3, #N] ; str r2, [r3, #N] ; bx lr': [
        ('void f(void)', '*(int*) ((char*) {D0} + {N1}) = {N0}; *(int*) ((char*) {D0} + {N2}) = {N0};')],
    'ldr r3, [r0, #N] ; ldrb r2, [r1] ; strb r2, [r3, #N] ; bx lr': [
        (f'void f({P}, const unsigned char* q)', '(*(char**) (p + {N0}))[{N1}] = *q;')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r3, [r3, #N] ; ldr r0, [r3, #N] ; bx lr': [
        ('int f(void)', 'return ((int*) *(char**) ((char*) {D0} + {N0}))[{V1}];')],
    'cmp r0, #N ; bne #N ; b X ; b X': [
        ('void f(int a)', 'if (a != {N0}) {F1}(); else {F0}();'),
        ('int f(int a)', 'if (a != {N0}) return {F1}(); return {F0}();')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; mov r1, #N ; ldr r0, [r3] ; b X': [
        ('int f(void)', 'return {F2}(*(int*) {D0}, {N0});')],
    'push {r3, lr} ; mov r3, r0 ; ldr r3, [r3, #N] ; add r0, r0, #N ; ldr r3, [r3, #N] ; blx r3 ; pop {r3, pc}': [
        ('void f(char* p)', 'reinterpret_cast<VV*> (p + {N0})->v{V2} ();')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r0, [r3] ; b X': [
        ('int f(void)', 'return {F2}(*(int*) {D0});')],
    'cmp r1, #N ; mov r0, #N ; moveq r1, #N ; movne r1, #N ; b X': [
        ('int f(int a, int b)', 'return {F0}({N1}, b == {N0} ? {N2} : {N3});'),
        ('int f(int a, int b)', 'return {F0}({N1}, b != {N0} ? {N3} : {N2});')],
    'ldr r0, [r0, #N] ; cmp r0, #N ; ldrbne r0, [r0, #N] ; bx lr': [
        (f'int f({P})', 'char* q = *(char**) (p + {N0}); return q ? *(unsigned char*) (q + {N2}) : 0;')],
    'push {r3, lr} ; bl X ; mov r0, #N ; pop {r3, pc}': [
        (f'int f({A4})', '{F0}(a, b, c, d); return {N0};')],
    'cmp r1, #N ; mvngt r0, #N ; movle r0, #N ; bx lr': [
        ('int f(int a, int b)', 'return b > {N0} ? ~{N1} : {N2};')],
    'push {r3, lr} ; mov r0, #N ; bl X ; mov r0, #N ; pop {r3, pc}': [
        ('int f(void)', '{F0}({N0}); return {N1};')],
    'push {r3, lr} ; ldr r0, [r0, #N] ; bl X ; mov r0, #N ; pop {r3, pc}': [
        (f'int f({P}, int b, int c, int d)', '{F0}(*(char**) (p + {N0}), b, c, d); return {N1};')],
    'push {r3, lr} ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; pop {r3, pc}': [
        ('int f(VO* p, int b, int c)', 'return (int) p->v{V0}(b, c);'),
        ('bool f(VB* p, int b, int c)', 'return p->v{V0}(b, c);')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; strb r1, [r3, #N] ; strb r2, [r3, #N] ; bx lr': [
        ('void f(int a, int b, int c)', '((char*) {D0})[{N0}] = b; ((char*) {D0})[{N1}] = c;')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; movw r2, #N ; str r0, [r3, r2] ; bx lr': [
        ('void f(int a)', '*(int*) ((char*) {D0} + {N0}) = a;')],
    'ldr r3, [r0, #N] ; cmp r3, #N ; ldrne r0, [r3, #N] ; moveq r0, #N ; bx lr': [
        (f'int f({P})', 'char* q = *(char**) (p + {N0}); return q ? *(int*) (q + {N2}) : {N3};'),
        (f'int f({P})', 'char* q = *(char**) (p + {N0}); if (q) return *(int*) (q + {N2}); return {N3};'),
        (f'float f({P})', 'char* q = *(char**) (p + {N0}); return q ? *(float*) (q + {N2}) : {N3};')],
    'ldr r2, [r0, #N] ; ldr r3, [r0, #N] ; strb r1, [r2, #N] ; strb r1, [r3, #N] ; bx lr': [
        (f'void f({P}, bool v)', '(*(char**) (p + {N0}))[{N2}] = v; (*(char**) (p + {N1}))[{N3}] = v;'),
        (f'void f({P}, bool v)', 'char* a = *(char**) (p + {N0}); char* b = *(char**) (p + {N1}); a[{N2}] = v; b[{N3}] = v;')],
    'ldr r0, [r0, #N] ; cmp r0, #N ; bxeq lr ; mov r1, #N ; mov r2, #N ; b X': [
        (f'int f({P})', 'char* q = *(char**) (p + {N0}); if (!q) return 0; return {F0}(q, {N2}, {N3});')],
    'push {r4, lr} ; mov r4, r0 ; bl X ; mov r0, r4 ; pop {r4, lr} ; b X': [
        ('int f(int a)', '{F0}(a); return {F1}(a);')],
    'ldr r3, [r0, #N] ; cmp r3, #N ; bxle lr ; b X': [
        (f'void f({P})', 'if (*(int*) (p + {N0}) > {N1}) {F0}(p);')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldrh r0, [r3, #N] ; bx lr': [
        ('int f(void)', 'return *(unsigned short*) ((char*) {D0} + {N0});')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; movw r2, #N ; ldr r0, [r3, r2] ; bx lr': [
        ('int f(void)', 'return *(int*) ((char*) {D0} + {N0});')],
    'push {r3, lr} ; mov r0, r1 ; bl X ; cmp r0, #N ; ldrbne r0, [r0, #N] ; pop {r3, pc}': [
        ('int f(int a, int b)', 'unsigned char* q = (unsigned char*) {F0}(b); return q ? q[{N1}] : 0;')],
    'cmp r1, #N ; ldrbeq r0, [r0, #N] ; ldrbne r0, [r0, #N] ; bx lr': [
        ('int f(unsigned char* p, int b)', 'return b == {N0} ? p[{N1}] : p[{N2}];')],
    'mov r1, r0 ; ldr r0, [r0, #N] ; cmp r0, #N ; bxeq lr ; add r1, r1, #N ; b X': [
        (f'int f({P})', 'char* q = *(char**) (p + {N0}); if (!q) return 0; return {F0}(q, p + {N2});')],
    'ldr r3, [r0, #N] ; str r1, [r3, #N] ; ldr r3, [r0, #N] ; str r1, [r3, #N] ; bx lr': [
        (f'void f({P}, void* v)', '*(void**) (*(char**) (p + {N0}) + {N1}) = v; *(void**) (*(char**) (p + {N2}) + {N3}) = v;')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; add r3, r3, r0, lsl #N ; ldr r0, [r3, #N] ; bx lr': [
        ('int f(int i)', 'return {D0}[i + {V1}];')],
    'push {r4, lr} ; mov r4, r0 ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        ('int f(int a)', '{F0}(a); return a;'),
        (f'int f({A4})', '{F0}(a, b, c, d); return a;')],
    'add r3, r0, #N ; ldr r0, [r0, #N] ; ldm r3, {r1, r2, r3} ; b X': [
        (f'int f({P})', 'int* q = (int*) (p + {N0}); return {F0}(*(int*) (p + {N1}), q[0], q[1], q[2]);')],
    'push {r3, lr} ; ldr r3, [r0, #N]! ; ldr r3, [r3, #N] ; blx r3 ; pop {r3, pc}': [
        (f'int f({P}, int b, int c)', 'return (int) ((VO*) (p + {N0}))->v{V1}(b, c);'),
        (f'bool f({P}, int b, int c)', 'return ((VB*) (p + {N0}))->v{V1}(b, c);'),
        ('void f(char* p)', 'typedef void (*Fn) (void*); char* o = p + {N0}; (*(Fn*) (*(char**) o + {N1})) (o);')],
    # bounds-checked lookups in a pointer array (index checked against a global count)
    'ldr r3, =X ; ldr r3, [r3] ; cmp r1, r3 ; bhs #N ; ldr r3, [r0, #N] ; ldr r0, [r3, r1, lsl #N] ; cmp r0, #N ; bxeq lr ; b X ; mov r0, #N ; bx lr': [
        (f'int f({P}, int i)', 'if ((unsigned) i >= *(unsigned*) {D0}) return {N4}; char* q = (*(char***) (p + {N1}))[i]; if (!q) return 0; return {F1}(q);')],
    'ldr r3, =X ; ldr r3, [r3] ; cmp r1, r3 ; bhs #N ; ldr r3, [r0, #N] ; ldr r0, [r3, r1, lsl #N] ; cmp r0, #N ; ldrbne r0, [r0, #N] ; bx lr ; mov r0, #N ; bx lr': [
        (f'int f({P}, int i)', 'if ((unsigned) i >= *(unsigned*) {D0}) return {N5}; char* q = (*(char***) (p + {N1}))[i]; return q ? *(unsigned char*) (q + {N4}) : 0;')],
    'ldr r3, =X ; ldr r3, [r3] ; cmp r1, r3 ; bhs #N ; ldr r3, [r0, #N] ; ldr r0, [r3, r1, lsl #N] ; cmp r0, #N ; bxeq lr ; mov r1, r2 ; b X ; mov r0, #N ; bx lr': [
        (f'int f({P}, int i, int c)', 'if ((unsigned) i >= *(unsigned*) {D0}) return {N4}; char* q = (*(char***) (p + {N1}))[i]; if (!q) return 0; return {F1}(q, c);')],
    'push {r3, lr} ; ldr r3, =X ; ldr r3, [r3] ; cmp r1, r3 ; bhs #N ; ldr r3, [r0, #N] ; ldr r0, [r3, r1, lsl #N] ; cmp r0, #N ; popeq {r3, pc} ; mov r1, r2 ; bl X ; mov r0, #N ; pop {r3, pc} ; mov r0, #N ; pop {r3, pc}': [
        (f'int f({P}, int i, int c)', 'if ((unsigned) i >= *(unsigned*) {D0}) return {N5}; char* q = (*(char***) (p + {N1}))[i]; if (!q) return 0; {F1}(q, c); return {N4};')],
    'ldr r3, =X ; mov r1, r0 ; ldr r3, [r3] ; cmp r3, #N ; bxeq lr ; ldr r0, [r3, #N] ; cmp r0, #N ; bxeq lr ; b X': [
        ('void f(int a)', 'char* g = *(char**) {D0}; if (!g) return; char* t = *(char**) (g + {N1}); if (!t) return; {F1}(t, a);')],
    'movw r0, #:lower16:X ; movt r0, #:upper16:X ; push {r3, lr} ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; pop {r3, pc}': [
        ('int f(void)', 'return (int) ((VN*) {D0})->v{V0}();'),
        ('int f(int a, int b, int c)', 'return (int) ((VO*) {D0})->v{V0}(b, c);')],
    # vtable stores (destructor fragments)
    'ldr r3, =X ; push {r4, lr} ; mov r4, r0 ; str r3, [r0] ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        ('void* f(void** p)', 'p[0] = (void*) {D0}; {F1}(p); return p;')],
    'push {r4, lr} ; mov r4, r0 ; bl X ; mov r0, r4 ; movw r3, #:lower16:X ; movt r3, #:upper16:X ; str r3, [r4] ; pop {r4, lr} ; b X': [
        ('int f(void** p)', '{F0}(p); p[0] = (void*) {D1}; return {F3}(p);')],
    'ldr r2, =X ; str r2, [r0, #N] ; ldr r2, =X ; str r2, [r0] ; bx lr': [
        ('void f(void** p)', 'p[{V0}] = (void*) {D0}; p[0] = (void*) {D1};'),
        ('void f(void** p)', 'p[0] = (void*) {D1}; p[{V0}] = (void*) {D0};')],
    'ldr r2, =X ; str r2, [r0] ; ldr r2, =X ; str r2, [r0, #N] ; bx lr': [
        ('void f(void** p)', 'p[0] = (void*) {D0}; p[{V0}] = (void*) {D1};'),
        ('void f(void** p)', 'p[{V0}] = (void*) {D1}; p[0] = (void*) {D0};')],
    'ldr r3, =X ; str r3, [r0] ; bx lr': [
        ('void f(void** p)', 'p[0] = (void*) {D0};')],
    'ldr r3, =X ; push {r4, lr} ; mov r4, r0 ; str r3, [r0, #N] ; ldr r3, =X ; str r3, [r0] ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        ('void* f(void** p)', 'p[{V0}] = (void*) {D0}; p[0] = (void*) {D1}; {F2}(p); return p;'),
        ('void* f(void** p)', 'p[0] = (void*) {D1}; p[{V0}] = (void*) {D0}; {F2}(p); return p;')],
    'ldr r3, =X ; push {r4, lr} ; mov r4, r0 ; str r3, [r0] ; ldr r3, =X ; str r3, [r0, #N] ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        ('void* f(void** p)', 'p[0] = (void*) {D0}; p[{V0}] = (void*) {D1}; {F2}(p); return p;'),
        ('void* f(void** p)', 'p[{V0}] = (void*) {D1}; p[0] = (void*) {D0}; {F2}(p); return p;')],
    # small helpers
    'ldr r0, [r0, #N] ; push {r3, lr} ; cmp r0, #N ; beq #N ; bl X ; mov r0, #N ; pop {r3, pc}': [
        (f'int f({P})', 'char* q = *(char**) (p + {N0}); if (q) {F0}(q); return {N3};')],
    'mov r3, #N ; strb r3, [r0, #N] ; bx lr': [
        (f'void f({P})', 'p[{N1}] = {N0};')],
    'str r1, [r0, #N] ; mov r0, #N ; bx lr': [
        (f'int f({P}, int v)', '*(int*) (p + {N0}) = v; return {N1};')],
    'push {r3, lr} ; bl X ; movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r3, [r3] ; subs r0, r0, r3 ; movne r0, #N ; pop {r3, pc}': [
        (f'bool f({A4})', 'return {F0}(a, b, c, d) != {D1}[0];')],
    'sub r0, r0, #N ; cmp r0, #N ; ldrls r3, =X ; movhi r0, #N ; addls r0, r3, r0, lsl #N ; ldrls r0, [r0, #N] ; bx lr': [
        ('int f(unsigned i)', 'i -= {N0}; if (i > {N1}) return {N2}; return *(int*) ((char*) {D0} + (i << {N3}) + {N4});')],
    'ldmib r0, {r0, r1} ; b X': [
        ('int f(int* p)', 'return {F0}(p[1], p[2]);')],
    'movw r0, #:lower16:X ; movt r0, #:upper16:X ; b X': [
        ('int f(void)', 'return {F2}({D0});')],
    'push {r3, lr} ; movw r0, #:lower16:X ; movt r0, #:upper16:X ; bl X ; mov r0, #N ; pop {r3, pc}': [
        ('int f(void)', '{F2}({D0}); return {N0};')],
    'ldrb r0, [r0, #N] ; ubfx r0, r0, #N, #N ; bx lr': [
        ('int f(unsigned char* p)', 'return (p[{N0}] >> {N1}) & ((1 << {N2}) - 1);')],
    'push {r4, lr} ; add r1, r1, #N ; mov r4, r0 ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        ('void* f(void* p, char* q)', '{F0}(p, q + {N0}); return p;')],
    'push {r4, lr} ; movw r1, #:lower16:X ; mov r4, r0 ; movt r1, #:upper16:X ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        ('void* f(void* p)', '{F2}(p, {D0}); return p;')],
    'ldr r0, [r0, #N] ; push {r3, lr} ; cmp r0, #N ; popeq {r3, pc} ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; pop {r3, pc}': [
        (f'int f({P}, int b, int c)', 'VO* q = *(VO**) (p + {N0}); if (!q) return 0; return (int) q->v{V2}(b, c);'),
        (f'bool f({P}, int b, int c)', 'VB* q = *(VB**) (p + {N0}); if (!q) return 0; return q->v{V2}(b, c);')],
    'ldr r0, =X ; bx lr': [
        ('void* f(void)', 'return {D0};')],
    'push {r3, lr} ; ldr r3, =X ; ldr r3, [r3, #N] ; cmp r3, #N ; popeq {r3, pc} ; blx r3 ; pop {r3, pc}': [
        ('int f(void)', 'FnL fn = *(FnL*) ((char*) {D0} + {N0}); if (!fn) return 0; return (int) fn();'),
        ('void f(void)', 'typedef void (*FnV) (); FnV fn = *(FnV*) ((char*) {D0} + {N0}); if (fn) fn();')],
    # UiTimer-derived destructors: stop a running timer, clear the id
    'ldr r3, [r0, #N] ; push {r4, lr} ; cmp r3, #N ; ldr r3, =X ; mov r4, r0 ; str r3, [r0] ; ble #N ; bl X ; mov r3, #N ; mov r0, r4 ; str r3, [r4, #N] ; pop {r4, pc}': [
        ('void* f(void** p)', 'p[0] = (void*) {D0}; if (*(int*) ((char*) p + {N0}) > {N1}) {F1}(p); *(int*) ((char*) p + {N4}) = {N3}; return p;')],
    'ldr r3, [r0, #N] ; push {r4, lr} ; cmp r3, #N ; ldr r3, =X ; mov r4, r0 ; str r3, [r0] ; ble #N ; bl X ; mov r0, r4 ; mov r3, #N ; str r3, [r4, #N] ; bl X ; mov r0, r4 ; pop {r4, pc}': [
        ('void* f(void** p)', 'p[0] = (void*) {D0}; if (*(int*) ((char*) p + {N0}) > {N1}) {F1}(p); *(int*) ((char*) p + {N4}) = {N3}; {F2}(p); return p;')],
    'push {r4, lr} ; ldr r4, =X ; ldr r3, [r4, #N] ; cmp r3, #N ; beq #N ; ldr r0, =X ; bl X ; mov r3, #N ; str r3, [r4, #N] ; ldr r0, =X ; pop {r4, pc}': [
        ('void* f(void)', 'if (*(int*) ((char*) {D0} + {N0})) {{ {F2}({D1}); *(int*) ((char*) {D0} + {N4}) = {N3}; }} return {D3};')],
    'str lr, [sp, #N]! ; sub sp, sp, #N ; mov lr, r1 ; mov ip, r2 ; str r3, [sp] ; movw r1, #N ; mov r2, lr ; mov r3, ip ; bl X ; add sp, sp, #N ; ldm sp!, {pc}': [
        (f'int f({A4})', 'return {F0}(a, {N2}, b, c, d);')],
    'mov r2, r1 ; ldr r1, =X ; push {r3, lr} ; mov r3, r0 ; ldr r0, [r1] ; cmp r0, #N ; popeq {r3, pc} ; ldr ip, [r0] ; adds r1, r3, #N ; movne r1, #N ; ldr r3, [ip, #N] ; blx r3 ; pop {r3, pc}': [
        ('int f(int a, int b)', 'VO* g = *(VO**) {D0}; if (!g) return 0; return (int) g->v{V3}(a != -{N1}, b);')],
    'push {r3, lr} ; cmp r0, #N ; ldr r3, =X ; moveq r1, #N ; movne r1, #N ; ldr r0, [r3] ; cmp r0, #N ; popeq {r3, pc} ; ldr r0, [r0, #N] ; cmp r0, #N ; popeq {r3, pc} ; bl X ; pop {r3, pc}': [
        ('int f(int a)', 'char* g = *(char**) {D0}; if (!g) return 0; char* t = *(char**) (g + {N4}); if (!t) return 0; return (int) {L1}(t, a == {N0} ? {N1} : {N2});'),
        ('int f(int a)', 'char* g = *(char**) {D0}; if (!g) return 0; char* t = *(char**) (g + {N4}); if (!t) return 0; return {B1}(t, a == {N0} ? {N1} : {N2});')],
    'push {r3, lr} ; movw r3, #:lower16:X ; movt r3, #:upper16:X ; mov r2, #N ; mov r0, #N ; movw r1, #:lower16:X ; str r2, [r3] ; movt r1, #:upper16:X ; bl X ; mov r0, #N ; pop {r3, pc}': [
        ('int f(void)', '{D0}[0] = {N0}; {F4}({N1}, (char*) {D2}); return {N2};')],
    'add r0, r0, r0, lsl #N ; movw r3, #:lower16:X ; movt r3, #:upper16:X ; movw r2, #N ; add r3, r3, r0, lsl #N ; ldr r0, [r3, r2] ; bx lr': [
        ('int f(int i)', 'return *(int*) ((char*) {D0} + i * (((1 << {N0}) + 1) << {N2}) + {N1});')],
    'push {r4, r5, r6, lr} ; mov r4, r0 ; ldr r0, [r0, #N] ; mov r6, r1 ; bl X ; mov r1, r6 ; mov r5, r0 ; ldr r0, [r4, #N] ; bl X ; and r0, r0, r5 ; uxtb r0, r0 ; pop {r4, r5, r6, pc}': [
        (f'bool f({P}, int b)', 'bool x = {B0}(*(char**) (p + {N0}), b); return x & {B1}(*(char**) (p + {N1}), b);')],
    'mov r3, r2 ; ldr r2, =X ; push {r4, lr} ; mov r4, r0 ; ldr r0, [r2] ; mov ip, r1 ; cmp r0, #N ; popeq {r4, pc} ; ldr lr, [r0] ; adds r1, r4, #N ; mov r2, ip ; movne r1, #N ; ldr ip, [lr, #N] ; blx ip ; pop {r4, pc}': [
        ('int f(int a, int b, int c)', 'V3* g = *(V3**) {D0}; if (!g) return 0; return (int) g->v{V3}(a != -{N1}, b, c);')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r0, [r3, #N] ; sub ip, r0, #N ; rsbs r0, ip, #N ; adc r0, r0, ip ; bx lr': [
        ('bool f(void)', 'return *(int*) ((char*) {D0} + {N0}) == {N1};')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r0, [r3, #N] ; sub r3, r0, #N ; rsbs r0, r3, #N ; adc r0, r0, r3 ; bx lr': [
        ('bool f(void)', 'return *(int*) ((char*) {D0} + {N0}) == {N1};')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; ldr r0, [r3, #N] ; sub r2, r0, #N ; rsbs r0, r2, #N ; adc r0, r0, r2 ; bx lr': [
        ('bool f(void)', 'return *(int*) ((char*) {D0} + {N0}) == {N1};')],
    'push {r3, lr} ; ldr r0, [r0, #N] ; bl X ; cmp r0, #N ; mvneq r0, #N ; movne r0, #N ; pop {r3, pc}': [
        (f'int f({P})', 'return {F0}(*(char**) (p + {N0})) == {N1} ? ~{N2} : {N3};')],
    'cmp r0, #N ; ldrls r3, =X ; ldrhi r0, =X ; ldrls r0, [r3, r0, lsl #N] ; bx lr': [
        ('const char* f(unsigned i)', 'return i <= {N0} ? ((const char**) {D0})[i] : (const char*) {D1};')],
    'push {r3, lr} ; ldr r3, [r0, #N] ; ldr r0, [r3, #N] ; cmp r0, #N ; popeq {r3, pc} ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; pop {r3, pc}': [
        (f'int f({P}, int b, int c)', 'VO* q = *(VO**) (*(char**) (p + {N0}) + {N1}); if (!q) return 0; return (int) q->v{V3}(b, c);'),
        (f'bool f({P}, int b, int c)', 'VB* q = *(VB**) (*(char**) (p + {N0}) + {N1}); if (!q) return 0; return q->v{V3}(b, c);')],
    'push {r4, lr} ; mov r4, r0 ; bl X ; ldr r3, =X ; mov r0, r4 ; str r3, [r4] ; pop {r4, pc}': [
        ('void* f(void** p)', '{F0}(p); p[0] = (void*) {D1}; return p;')],
    'movw r3, #:lower16:X ; movt r3, #:upper16:X ; movw r1, #N ; ldr r2, [r3, #N] ; mla r3, r1, r2, r3 ; ldr r0, [r3, #N] ; bx lr': [
        ('int f(void)', 'return *(int*) ((char*) {D0} + *(int*) ((char*) {D0} + {N1}) * {N0} + {N2});')],
    'push {r3, r4, r5, lr} ; movw r4, #N ; mov r5, r0 ; ldr r0, [r0, r4] ; cmp r0, #N ; popeq {r3, r4, r5, pc} ; bl X ; mov r3, #N ; str r3, [r5, r4] ; pop {r3, r4, r5, pc}': [
        (f'void f({P})', 'char* q = *(char**) (p + {N0}); if (!q) return; {F0}(q); *(char**) (p + {N0}) = 0;')],
    'push {r4, lr} ; mov r4, r0 ; ldr r0, [r0, #N] ; cmp r0, #N ; beq #N ; bl X ; mov r3, #N ; str r3, [r4, #N] ; ldr r0, [r4, #N] ; cmp r0, #N ; popeq {r4, pc} ; bl X ; mov r3, #N ; str r3, [r4, #N] ; pop {r4, pc}': [
        (f'void f({P})', 'if (*(char**) (p + {N0})) {{ {F0}(*(char**) (p + {N0})); *(char**) (p + {N4}) = 0; }} if (*(char**) (p + {N5})) {{ {F1}(*(char**) (p + {N5})); *(char**) (p + {N8}) = 0; }}')],
    'push {r4, lr} ; mov r4, r0 ; ldr r0, [r0, #N] ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; ldr r0, [r4, #N] ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; ldr r0, [r4, #N] ; ldr r3, [r0] ; ldr r3, [r3, #N] ; blx r3 ; pop {r4, pc}': [
        (f'int f({P})', '(*(VN**) (p + {N0}))->v{V1}(); (*(VN**) (p + {N2}))->v{V3}(); return (int) (*(VN**) (p + {N4}))->v{V5}();')],
    # results of calls turned into bools / compared
    'push {r3, lr} ; bl X ; adds r0, r0, #N ; movne r0, #N ; pop {r3, pc}': [
        (f'bool f({A4})', 'return {F0}(a, b, c, d) != -{N0};')],
    'push {r3, lr} ; bl X ; rsbs r0, r0, #N ; movlo r0, #N ; pop {r3, pc}': [
        (f'bool f({A4})', 'return {F0}(a, b, c, d) == {N0} - 1;'),
        (f'bool f({A4})', 'return (unsigned) {F0}(a, b, c, d) < {N0};')],
    'push {r3, lr} ; bl X ; mvn r0, r0 ; lsr r0, r0, #N ; pop {r3, pc}': [
        (f'bool f({A4})', 'return {F0}(a, b, c, d) >= 0;')],
    'push {r3, lr} ; bl X ; lsr r0, r0, #N ; pop {r3, pc}': [
        (f'bool f({A4})', 'return {F0}(a, b, c, d) < 0;'),
        (f'unsigned f({A4})', 'return (unsigned) {F0}(a, b, c, d) >> {N0};')],
    'push {r3, lr} ; bl X ; cmp r0, #N ; movgt r0, #N ; movle r0, #N ; pop {r3, pc}': [
        (f'int f({A4})', 'return {F0}(a, b, c, d) > {N0} ? {N1} : {N2};')],
    'push {r3, lr} ; bl X ; cmp r0, #N ; movle r0, #N ; movgt r0, #N ; pop {r3, pc}': [
        (f'int f({A4})', 'return {F0}(a, b, c, d) <= {N0} ? {N1} : {N2};'),
        (f'int f({A4})', 'return {F0}(a, b, c, d) > {N0} ? {N2} : {N1};')],
    # function-pointer members
    'push {r3, lr} ; ldr r3, [r0, #N] ; ldr r0, [r0, #N] ; blx r3 ; pop {r3, pc}': [
        (f'int f({P}, int b, int c)', 'return (int) (*(FnL3*) (p + {N0})) (*(void**) (p + {N1}), b, c);')],
    'push {r3, lr} ; ldr r3, [r0, #N] ; blx r3 ; pop {r3, pc}': [
        (f'int f({P}, int b, int c)', 'return (int) (*(FnL3*) (p + {N0})) (p, b, c);')],
    'mov r3, r0 ; ldr r0, [r0, #N] ; ldr r1, [r3, #N] ; bx lr': [
        ('long long f(void* p)', 'struct S {{ char pad[{N0}]; long long v; }}; return ((S*) p)->v;')],
    'cmp r0, #N ; bxeq lr ; b X': [
        ('void f(void* p)', 'if (p != 0) {F0}(p);'),
        ('int f(int a)', 'if (a == {N0}) return a; return {F0}(a);')],
    'ldr r3, =X ; mov r2, #N ; add r3, r0, r3 ; strb r2, [r3, #N] ; bx lr': [
        (f'void f({P})', 'p[{A0} + {N1}] = {N0};')],
    # per-deck getters: an object with two deck pointers returns a field of deck 0 or 1
    'ldr r3, [r0, #N] ; cmp r3, #N ; moveq r0, r3 ; bxeq lr ; ldr r0, [r0, #N] ; cmp r0, #N ; bxeq lr ; cmp r1, #N ; ldreq r0, [r3, #N] ; bxeq lr ; cmp r1, #N ; beq #N ; mov r0, #N ; bx lr ; ldr r0, [r0, #N] ; bx lr': [
        (f'int f({P}, int deck)', 'char* a = *(char**) (p + {N0}); if (!a) return 0; char* b = *(char**) (p + {N2}); if (!b) return 0; '
                                  'switch (deck) {{ case 0: return *(int*) (a + {N5}); case 1: return *(int*) (b + {N9}); default: return 0; }}'),
        (f'int f({P}, int deck)', 'char* a = *(char**) (p + {N0}); if (!a) return 0; char* b = *(char**) (p + {N2}); if (!b) return 0; '
                                  'if (deck == 0) return *(int*) (a + {N5}); if (deck == 1) return *(int*) (b + {N9}); return 0;')],
    'ldr r3, [r0, #N] ; cmp r3, #N ; moveq r0, r3 ; bxeq lr ; ldr r0, [r0, #N] ; cmp r0, #N ; bxeq lr ; cmp r1, #N ; beq #N ; cmp r1, #N ; beq #N ; mov r0, #N ; bx lr ; ldrb r0, [r3, #N] ; ubfx r0, r0, #N, #N ; bx lr ; ldrb r0, [r0, #N] ; ubfx r0, r0, #N, #N ; bx lr': [
        (f'bool f({P}, int deck)', 'char* a = *(char**) (p + {N0}); if (!a) return 0; char* b = *(char**) (p + {N2}); if (!b) return 0; '
                                   'switch (deck) {{ case 0: return (*(unsigned char*) (a + {N9}) >> {N10}) & 1; case 1: return (*(unsigned char*) (b + {N12}) >> {N13}) & 1; default: return 0; }}'),
        (f'bool f({P}, int deck)', 'char* a = *(char**) (p + {N0}); if (!a) return 0; char* b = *(char**) (p + {N2}); if (!b) return 0; '
                                   'if (deck == 0) return (*(unsigned char*) (a + {N9}) >> {N10}) & 1; if (deck == 1) return (*(unsigned char*) (b + {N12}) >> {N13}) & 1; return 0;')],
    # store a new value if it is in range and changed, then notify
    'push {r3, lr} ; ldr r3, [r0, #N] ; ldrh r1, [r0, #N] ; uxth r2, r3 ; cmp r1, r2 ; movls r0, #N ; popls {r3, pc} ; ldr r2, [r0, #N] ; cmp r3, r2 ; beq #N ; str r3, [r0, #N] ; mov r0, r3 ; bl X ; mov r0, #N ; pop {r3, pc} ; mov r0, #N ; pop {r3, pc}': [
        (f'bool f({P})', 'int* next = (int*) (p + {N0}); int* cur = (int*) (p + {N3}); '
                         'if ((unsigned short) *next >= *(unsigned short*) (p + {N1})) return false; '
                         'if (*next == *cur) return false; *cur = *next; {F0}(*cur); return true;')],
}

SYM = re.compile(r'\b(?:[fbdcp]_\w+|leaf_\w+|_Z\w+|[A-Za-z_]\w*_[0-9a-f]{4,})\b')
NUM = re.compile(r'#(-?(?:0x[0-9a-f]+|\d+))')

starts = sorted(int(l.split()[0], 16) for l in open('split/functions.txt') if l.startswith('0x'))
# EXTRA_STARTS: a file of further function starts (hiddenstarts.py) inside split functions
if __import__('os').environ.get('EXTRA_STARTS'):
    starts = sorted(set(starts) | {int(l.split()[0], 16) for l in open(__import__('os').environ['EXTRA_STARTS']) if l.strip()})
label_addr = {}
for l in open('split/labels.txt'):
    q = l.split()
    if len(q) == 2:
        label_addr.setdefault(q[1], int(q[0], 16))
size = {a: b - a for a, b in zip(starts, starts[1:])}
grafted = {int(l.split()[0], 16) for l in open('grafts.txt') if l.strip() and l[0] != '#'
           and l.split()[2] != OUT}

# instructions per function from one listing of the whole range
listing = subprocess.run(['.venv/bin/python', os.path.join(os.path.dirname(__file__), 'listing.py'),
                          '0xeda0', '0x360000'], capture_output=True, text=True).stdout
insns = {}
for line in listing.split('\n'):
    m = re.match(r'^  ([0-9a-f]+): (.*)$', line)
    if m:
        insns[int(m.group(1), 16)] = m.group(2).strip()
        continue
    m = re.match(r'^  (.*?)\s*@ ([0-9a-f]+)$', line)
    if m:
        insns[int(m.group(2), 16)] = m.group(1).strip()


def shape(a):
    ins = []
    for x in range(a, a + size[a], 4):
        t = insns.get(x, '')
        if t.startswith('.word'):
            break
        m = re.search(r'\[pc(?:, #(0x[0-9a-f]+|\d+))?\]', t)
        if m:   # a literal-pool load: name the pool word's symbol (ldr rX, =sym)
            word = insns.get(x + 8 + int(m.group(1) or '0', 0), '')
            if not word.startswith('.word ') or not SYM.fullmatch(word[6:].strip()):
                return '', [], []
            t = t[:m.start()] + '=' + word[6:].strip() + t[m.end():]
        ins.append(t)
    text = ' ; '.join(ins)
    syms = SYM.findall(text)
    nums = NUM.findall(SYM.sub('X', text))
    norm = NUM.sub('#N', SYM.sub('X', text))
    return norm, nums, syms


def render(a, sig, body, nums, syms, decls):
    fill = {}
    for i, n in enumerate(nums):
        fill[f'N{i}'] = n
        fill[f'V{i}'] = int(n, 0) // 4
    for i, s in enumerate(syms):
        fill[f'F{i}'] = f'fn_{a:x}_{i}'
        fill[f'L{i}'] = f'fnl_{a:x}_{i}'
        fill[f'B{i}'] = f'fnb_{a:x}_{i}'
        fill[f'A{i}'] = hex(label_addr.get(s, 0))
        fill[f'D{i}'] = f'data_{a:x}_{i}'
    used = set(re.findall(r'\{([FLDB]\d+)\}', body))
    for u in sorted(used):
        s = syms[int(u[1:])]
        kind = {'F': 'int {}(...)', 'L': 'long long {}(...)', 'D': 'int {}[]', 'B': 'bool {}(...)'}[u[0]]
        decls.append(f'extern {kind.format(fill[u])} __asm__ ("{s}");')
    name = f'shape_{a:x}'
    return f'{sig.replace(" f(", f" {name}(")}\n{{\n    {body.format(**fill)}\n}}\n'


def build(path, items):
    text = ['// Generated by autoshape.py: small functions rebuilt from instruction shapes.',
            '// Names are placeholders.', '', VT, '', 'extern "C" {', '']
    for decls, fn in items:
        text += decls + [fn]
    text.append('}')
    open(path, 'w').write('\n'.join(text) + '\n')


if '--survey' in sys.argv:   # the most common shapes without a table entry
    from collections import Counter
    count, example = Counter(), {}
    for a in starts:
        if a not in grafted and a in size and 0xeda0 <= a < 0x360000 and size[a] <= int(__import__("os").environ.get("SHAPE_MAX", "128")):
            norm = shape(a)[0]
            if norm not in TABLE and norm:
                count[norm] += 1
                example.setdefault(norm, a)
    for norm, n in count.most_common(int(sys.argv[-1]) if sys.argv[-1].isdigit() else 40):
        print(f'{n:3} {example[norm]:#x}  {norm}')
    sys.exit()

cands = {}
for a in starts:
    if a in grafted or a not in size or not 0xeda0 <= a < 0x360000 or size[a] > int(__import__("os").environ.get("SHAPE_MAX", "128")):
        continue
    norm, nums, syms = shape(a)
    if norm in TABLE:
        temps = TABLE[norm]
        if '=X' in norm:   # literal pools also come from code GCC optimised for size (cold)
            temps = temps + [('__attribute__((cold)) ' + sig, body) for sig, body in temps]
        cands[a] = (temps, nums, syms)
print(len(cands), 'candidates')

found = {}
for i in range(max((len(c[0]) for c in cands.values()), default=0)):
    todo = [a for a in cands if a not in found and i < len(cands[a][0])]
    items = []
    for a in todo:
        decls = []
        sig, body = cands[a][0][i]
        items.append((decls, render(a, sig, body, cands[a][1], cands[a][2], decls)))
    build('obj/_autoshape.cpp', items)
    r = subprocess.run(['./cc11.sh', 'obj/_autoshape.cpp', 'obj/_autoshape.o'] + FLAGS,
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr[-3000:])
    out = subprocess.run(['.venv/bin/python', '../tools/decomp/match.py', 'rbp.orig', 'obj/_autoshape.o']
                         + [f'shape_{a:x}={a:#x}' for a in todo], capture_output=True, text=True).stdout
    ok = {l.split()[1] for l in out.split('\n') if l.startswith('MATCH')}
    for a in todo:
        if f'shape_{a:x}' in ok:
            found[a] = i
    print(f'round {i}: {len(todo)} tried, {len(found)} found', flush=True)

items = []
for a in sorted(found):
    decls = []
    sig, body = cands[a][0][found[a]]
    items.append((decls, render(a, sig, body, cands[a][1], cands[a][2], decls)))
build(OUT, items)
open(OUT.replace('src/auto/', 'obj/').replace('.cpp', '.pairs'), 'w').write(''.join(f'shape_{a:x}={a:#x}\n' for a in sorted(found)))
for norm in TABLE:   # report shapes that never match, to improve their templates
    tried = [a for a in cands if cands[a][0][0] is TABLE[norm][0]]
    miss = [a for a in tried if a not in found]
    if miss:
        print(f'{len(miss)}/{len(tried)} unmatched, e.g. {miss[0]:#x}: {norm}')
print(len(found), 'functions')
