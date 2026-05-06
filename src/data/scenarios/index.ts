import type { PromptResponse } from '../../types/prompt'
import { gatewayInstability } from './gatewayInstability'
import { fleetThermal } from './fleetThermal'
import { dispatchOptimization } from './dispatchOptimization'
import { v28Regression } from './v28Regression'
import { latencyCorrelation } from './latencyCorrelation'

export type ScenarioId =
  | 'gateway-instability'
  | 'fleet-thermal'
  | 'dispatch-optimization'
  | 'v28-regression'
  | 'latency-correlation'
  | 'fallback'

export function routePrompt(prompt: string): ScenarioId {
  const p = prompt.toLowerCase()
  if (/gateway[\s-]?17|unstable|instability|restart/.test(p)) return 'gateway-instability'
  if (/thermal|fleet|abnormal|heat/.test(p)) return 'fleet-thermal'
  if (/dispatch|schedule|optim(al|i[sz]e)|soc|battery/.test(p)) return 'dispatch-optimization'
  if (/v2\.?8|regression|release|deploy/.test(p)) return 'v28-regression'
  if (/correl|latency|signal|root[\s-]?cause/.test(p)) return 'latency-correlation'
  return 'fallback'
}

export function runScenario(prompt: string): PromptResponse {
  const id = routePrompt(prompt)
  switch (id) {
    case 'gateway-instability':
      return gatewayInstability()
    case 'fleet-thermal':
      return fleetThermal()
    case 'dispatch-optimization':
      return dispatchOptimization()
    case 'v28-regression':
      return v28Regression()
    case 'latency-correlation':
      return latencyCorrelation()
    default:
      return {
        blocks: [
          {
            type: 'markdown',
            content:
              "I don't have data wired up for that prompt yet. Try one of these:\n\n" +
              '- *Why did gateway-17 become unstable yesterday?*\n' +
              '- *Show devices with abnormal thermal behavior this week.*\n' +
              '- *Find the optimal dispatch schedule while minimizing thermal stress.*\n' +
              '- *Did software release v2.8 increase CPU usage?*\n' +
              '- *What signals correlate most strongly with latency spikes?*',
          },
        ],
        metadata: { engine: 'mock' },
      }
  }
}

export const SUGGESTED_PROMPTS: { id: ScenarioId; text: string }[] = [
  { id: 'gateway-instability', text: 'Why did gateway-17 become unstable yesterday?' },
  { id: 'fleet-thermal', text: 'Show devices with abnormal thermal behavior this week.' },
  { id: 'dispatch-optimization', text: 'Find the optimal dispatch schedule while minimizing thermal stress.' },
  { id: 'v28-regression', text: 'Did software release v2.8 increase CPU usage?' },
  { id: 'latency-correlation', text: 'What signals correlate most strongly with latency spikes?' },
]
