// Optional local cross-language test. No network, database or hardware writes.
import { edit, exchange, initial, publish, snapshot } from '../../smart-home-solutions-t-by/supabase/functions/_shared/control-agreement.ts';
const text = await new Response(Deno.stdin.readable).text();
const input = JSON.parse(text), s = input.state ?? initial(input.home_id);
const result = input.action === 'sync' ? exchange(s, input.request, input.now) :
  { state: input.action === 'edit' ? edit(s, input.request) : publish(s, snapshot(s), input.request, input.now) };
console.log(JSON.stringify(result));
