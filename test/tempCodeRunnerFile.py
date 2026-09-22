    await activate_gold(update, ctx)


async def limited_cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cho phép nhiều người /check đồng thời trong giới hạn an toàn."""
    async with _check_slots:
        await cmd_check(update, ctx)


async def serialized_check_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Chỉ xử lý một file /chk tại một thời điểm."""
    async with _bulk_check_lock:
        await check_file(update, ctx)


