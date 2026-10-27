/* Synthetic firmware: check a 24 MHz counter and a ten-second virtual timer. */
.section .text.boot, "ax"
.global _start
_start:
    movz x19, #0x2001, lsl #16
    mov w0, #0x301
    str w0, [x19, #0x30]
    mrs x0, cntfrq_el0
    movz x1, #0x3600
    movk x1, #0x016e, lsl #16
    cmp x0, x1
    b.ne failed
    adr x1, begin
    bl print
    movz x0, #0x1c00
    movk x0, #0x0e4e, lsl #16
    msr cntv_tval_el0, x0
    mov x0, #1
    msr cntv_ctl_el0, x0
    isb
1:
    mrs x0, cntv_ctl_el0
    tbz x0, #2, 1b
    adr x1, end
    bl print
    b poweroff
failed:
    adr x1, failure
    bl print
poweroff:
    movz x0, #8
    movk x0, #0x8400, lsl #16
    hvc #0
    b poweroff
print:
    ldrb w2, [x1], #1
    cbz w2, 2f
    strb w2, [x19]
    b print
2:
    ret
begin: .asciz "CLOCK_BEGIN\n"
end: .asciz "CLOCK_END\n"
failure: .asciz "CLOCK_BAD_FREQUENCY\n"
