from src.feature.atom import Feature, MessageContext


class HelloFeature(Feature):
    name = "hello"
    title = "Hello"
    description = "收到 /hello 后回复 hello。"
    default_enabled = True

    async def handle(self, ctx: MessageContext) -> bool:
        if ctx.msg != "/hello":
            return False
        await ctx.bot.reply(ctx, "hello")
        return True
