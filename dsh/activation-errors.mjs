// Keep the useful exception across dsh's stopReason-only activation result.
// Do not copy requests, stacks, tool arguments or credentials into diagnostics.
export function activationError(error, env = process.env) {
  const messages = [];
  const seen = new Set();
  for (let item = error; item && !seen.has(item) && messages.length < 3; item = item.cause) {
    seen.add(item);
    messages.push(String(item.message ?? item));
  }
  let message = messages.join(' <- ') || 'DSH_ACTIVATION_ERROR: 分支执行失败，内核未提供异常原因';
  for (const [key, value] of Object.entries(env)) {
    if (/(?:KEY|TOKEN|PASSWORD|SECRET)/i.test(key) && value && value.length >= 4) message = message.split(value).join('[REDACTED]');
  }
  message = message.replace(/Bearer\s+[^\s"',;]+/gi, 'Bearer [REDACTED]')
    .replace(/\bsk-[a-zA-Z0-9_-]+/g, '[REDACTED]');
  // Provider errors sometimes embed an entire HTTP request. Retain the lead
  // diagnostic only, rather than reprinting conversation contents.
  message = message.split(/\b(?:request body|requestBody|messages["']?|prompt["']?|system["']?)\s*[:=]/i)[0];
  return message.replace(/[\r\n\t]+/g, ' ').slice(0, 1500);
}

export function observeActivationErrors(ctx, lookup) {
  return ctx.on('agent/error', ({agent, error}) => {
    const task = lookup(agent);
    if (task) task.activation_error = activationError(error);
  });
}
