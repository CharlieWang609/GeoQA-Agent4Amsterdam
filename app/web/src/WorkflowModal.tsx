// SPDX-License-Identifier: GPL-3.0-only

import { Fragment } from "react";

import { readableLabel } from "./jsonRecords";
import { Modal } from "./Modal";
import { workflowSteps } from "./retrievedData";
import type { JsonValue, SessionDraftVersion } from "./types";
import { deriveWorkflowDag, layoutWorkflowDag, type WorkflowDagLayout } from "./workflowDag";

// The declared data flow as one SVG when every ref resolves; otherwise the
// step list in declared order.
export function WorkflowModal({ draft, onClose }: { draft: SessionDraftVersion; onClose: () => void }) {
  const workflowDag = deriveWorkflowDag(draft);
  const workflowLayout = workflowDag ? layoutWorkflowDag(workflowDag) : null;
  const steps = workflowSteps(draft.concrete_workflow);
  return (
    <Modal title="Workflow" onClose={onClose}>
      {workflowLayout ? <WorkflowDiagram layout={workflowLayout} /> : (
        <section className="modal-workflow-section">
          <h3>Workflow steps</h3>
          <WorkflowChain steps={steps} />
        </section>
      )}
    </Modal>
  );
}

function WorkflowDiagram({ layout }: { layout: WorkflowDagLayout }) {
  return (
    <div className="workflow-dag-scroll">
      <svg
        className="workflow-dag"
        role="img"
        aria-label="Workflow data flow"
        width={layout.width}
        height={layout.height}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
      >
        <defs>
          <marker
            id="workflow-arrowhead"
            markerWidth="8"
            markerHeight="8"
            refX="7"
            refY="4"
            orient="auto"
            markerUnits="strokeWidth"
          >
            <path d="M 0 0 L 8 4 L 0 8 z" />
          </marker>
        </defs>
        {layout.edges.map((edge) => (
          <g className="workflow-edge" key={edge.id}>
            <path
              d={edge.path}
              markerEnd="url(#workflow-arrowhead)"
              data-edge-ref={edge.ref}
              data-edge-from={edge.from}
              data-edge-to={edge.to}
            />
            <text x={edge.labelX} y={edge.labelY} textAnchor="middle">
              {edge.labelLines.map((line, index) => (
                <Fragment key={index}>
                  {index > 0 ? " " : null}
                  <tspan x={edge.labelX} dy={index === 0 ? 0 : 12}>{line}</tspan>
                </Fragment>
              ))}
            </text>
          </g>
        ))}
        {layout.nodes.map((node) => (
          <g
            className={`workflow-node workflow-node-${node.kind}`}
            data-node-id={node.id}
            data-node-kind={node.kind}
            key={node.id}
            transform={`translate(${node.x} ${node.y})`}
          >
            <rect width={node.width} height={node.height} rx="8" />
            <text x={node.width / 2} y="22" textAnchor="middle">
              <tspan className="workflow-node-kind" x={node.width / 2}>{node.detail ?? (node.kind === "output" ? "Result" : node.kind)}</tspan>
              <tspan x={node.width / 2} dy="21">{node.label}</tspan>
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

function WorkflowChain({ steps }: { steps: Record<string, JsonValue>[] }) {
  return steps.length ? (
    <ol className="workflow-chain">
      {steps.map((step, index) => (
        <li key={`${String(step.step_id ?? "step")}-${index}`}>
          {readableLabel(String(step.algorithm_id ?? step.name ?? step.step_id ?? `Step ${index + 1}`))}
        </li>
      ))}
    </ol>
  ) : <p>Not available for this draft.</p>;
}
