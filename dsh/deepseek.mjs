// Reuse the official provider, credentials, replay and transport. Only repair
// the rc.2 projection edge where every declared tool becomes deferred.
import * as OfficialProvider from '@deepseek-ai/dsh-llm-deepseek-api-key';

export const name = 'qqbot-deepseek';
export const inject = OfficialProvider.inject;
export const Config = OfficialProvider.Config;

export function ensureImmediateTool(options) {
  if (!options.tools?.length || options.tools.some(tool => tool.deferLoading !== true)) return options;
  const anchor = options.tools.find(tool => tool.name === 'read') || options.tools[0];
  let offered = true; // The promoted declaration is available from the start.
  const messages = options.messages.flatMap(message => {
    if (message.role !== 'developer') return [message];
    const content = message.content.filter(block => {
      if (block.toolName !== anchor.name) return true;
      if (block.type === 'tool-removal') { offered = false; return true; }
      if (block.type !== 'tool-addition') return true;
      const redundant = offered;
      offered = true;
      return !redundant;
    });
    if (content.length === message.content.length) return [message];
    return content.length ? [{...message, content}] : [];
  });
  return {...options, messages, tools: options.tools.map(tool => tool === anchor ? {...tool, deferLoading:false} : tool)};
}

export function compatibleAdapter(adapter) {
  return new Proxy(adapter, {get(target, key) {
    // Both prepared loop calls and direct/auxiliary streams reach this seam
    // AFTER native tool-history projection. Preserve the bound model metadata.
    if (key === 'prepareCall') return async (...args) => {
      const prepared = await target.prepareCall(...args);
      return {...prepared, stream: options => prepared.stream(ensureImmediateTool(options))};
    };
    if (key === 'stream') return options => target.stream(ensureImmediateTool(options));
    const value = Reflect.get(target, key, target);
    return typeof value === 'function' ? value.bind(target) : value;
  }});
}

export function apply(ctx, config) {
  const llm = new Proxy(ctx.llm, {get(target, key) {
    if (key === 'registerAdapter') return (providers, adapter) => target.registerAdapter(providers, compatibleAdapter(adapter));
    const value = Reflect.get(target, key, target);
    return typeof value === 'function' ? value.bind(target) : value;
  }});
  const providerContext = new Proxy(ctx, {get(target, key) {
    if (key === 'llm') return llm;
    const value = Reflect.get(target, key, target);
    return typeof value === 'function' ? value.bind(target) : value;
  }});
  return OfficialProvider.apply(providerContext, config);
}
