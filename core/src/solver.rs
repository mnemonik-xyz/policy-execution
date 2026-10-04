//! Reviewer-free acceptance of a bounded, deterministic scheduling result.
//! This checker and its facts projection are tested, not covered by Verus.
use crate::{hash_tagged, valid_rule, valid_scope, Authorization, Hash, Request, Rule, Scope};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use warrant_verified_policy::{evaluate, Facts};

pub const MAX_JOBS: usize = 128;
pub const MAX_MACHINES: usize = 16;
pub const MAX_DEPENDENCIES: usize = 16;
pub const CHECKER_VERSION: u32 = 1;
pub const RESULT_TAG: &[u8] = b"warrant/schedule/v1";
pub const MAX_RESULT_BYTES: usize = RESULT_TAG.len() + 4 + MAX_JOBS * 16;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Machine {
    pub capacity: u32,
    /// Integer cost per resource unit per time tick, not the bounty token amount.
    pub price_per_unit_tick: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Job {
    pub units: u32,
    /// Duration on each machine. Zero means that machine cannot run this job.
    pub durations: Vec<u64>,
    pub release: u64,
    pub deadline: u64,
    /// Strictly increasing prior job indices: the instance is topologically ordered.
    pub dependencies: Vec<u32>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Instance {
    pub machines: Vec<Machine>,
    pub jobs: Vec<Job>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Assignment {
    pub job: u32,
    pub machine: u32,
    pub start: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SolverPolicy {
    pub version: u64,
    pub scope: Scope,
    pub valid_after: u64,
    pub valid_until: u64,
    pub checker_version: u32,
    pub instance_hash: Hash,
    pub max_cost: u64,
    pub rule: Rule,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SolverInput {
    pub policy: SolverPolicy,
    pub request: Request,
    pub instance: Instance,
    pub schedule: Vec<Assignment>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SolverAuthorization {
    pub authorization: Authorization,
    pub total_cost: u64,
}

impl SolverAuthorization {
    /// Thirteen static ABI words: the task authorization, then computed totalCost.
    /// A separate length prevents receipt tooling from selecting the old task image.
    pub fn journal(&self) -> Vec<u8> {
        let mut journal = self.authorization.journal();
        journal.extend_from_slice(&[0; 24]);
        journal.extend_from_slice(&self.total_cost.to_be_bytes());
        journal
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SolverError {
    InvalidInstance,
    InvalidPolicy,
    InvalidRequest,
    InstanceMismatch,
    ResultMismatch,
    IncompleteSchedule,
    InvalidAssignment,
    TimingViolation,
    DependencyViolation,
    CapacityExceeded,
    ArithmeticOverflow,
    CostExceeded,
    PolicyDenied,
}

impl std::fmt::Display for SolverError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for SolverError {}

pub fn instance_hash(instance: &Instance) -> Hash {
    hash_tagged(b"warrant/solver-instance/v1", instance)
}

pub fn policy_hash(policy: &SolverPolicy) -> Hash {
    hash_tagged(b"warrant/solver-policy/v1", policy)
}

/// Cross-language wire format: ASCII tag, u32 count, then (u32 job, u32 machine,
/// u64 start) for each job, all big-endian. No trailing bytes or optional fields.
pub fn result_bytes(schedule: &[Assignment]) -> Result<Vec<u8>, SolverError> {
    if schedule.is_empty() || schedule.len() > MAX_JOBS {
        return Err(SolverError::IncompleteSchedule);
    }
    let mut bytes = RESULT_TAG.to_vec();
    bytes.extend_from_slice(&(schedule.len() as u32).to_be_bytes());
    for (index, assignment) in schedule.iter().enumerate() {
        if assignment.job as usize != index {
            return Err(SolverError::InvalidAssignment);
        }
        bytes.extend_from_slice(&assignment.job.to_be_bytes());
        bytes.extend_from_slice(&assignment.machine.to_be_bytes());
        bytes.extend_from_slice(&assignment.start.to_be_bytes());
    }
    Ok(bytes)
}

pub fn result_hash(schedule: &[Assignment]) -> Result<Hash, SolverError> {
    Ok(Sha256::digest(result_bytes(schedule)?).into())
}

pub fn validate_instance(instance: &Instance) -> Result<(), SolverError> {
    if instance.jobs.is_empty()
        || instance.jobs.len() > MAX_JOBS
        || instance.machines.is_empty()
        || instance.machines.len() > MAX_MACHINES
        || instance
            .machines
            .iter()
            .any(|m| m.capacity == 0 || m.price_per_unit_tick == 0)
    {
        return Err(SolverError::InvalidInstance);
    }
    for (index, job) in instance.jobs.iter().enumerate() {
        if job.units == 0
            || job.release >= job.deadline
            || job.durations.len() != instance.machines.len()
            || job.durations.iter().all(|duration| *duration == 0)
            || job.dependencies.len() > MAX_DEPENDENCIES
            || job.dependencies.iter().any(|dep| *dep as usize >= index)
            || job.dependencies.windows(2).any(|pair| pair[0] >= pair[1])
        {
            return Err(SolverError::InvalidInstance);
        }
    }
    Ok(())
}

/// Non-preemptive jobs occupy half-open intervals [start, end).
/// Capacity can increase only at a start, so checking every start is sufficient.
pub fn check_schedule(instance: &Instance, schedule: &[Assignment]) -> Result<u64, SolverError> {
    validate_instance(instance)?;
    if schedule.len() != instance.jobs.len() {
        return Err(SolverError::IncompleteSchedule);
    }
    let mut ends = Vec::with_capacity(schedule.len());
    let mut cost = 0u64;
    for (index, assignment) in schedule.iter().enumerate() {
        if assignment.job as usize != index
            || assignment.machine as usize >= instance.machines.len()
        {
            return Err(SolverError::InvalidAssignment);
        }
        let job = &instance.jobs[index];
        let machine = &instance.machines[assignment.machine as usize];
        let duration = job.durations[assignment.machine as usize];
        if duration == 0 {
            return Err(SolverError::InvalidAssignment);
        }
        let end = assignment
            .start
            .checked_add(duration)
            .ok_or(SolverError::ArithmeticOverflow)?;
        if assignment.start < job.release || end > job.deadline {
            return Err(SolverError::TimingViolation);
        }
        if job
            .dependencies
            .iter()
            .any(|dep| ends[*dep as usize] > assignment.start)
        {
            return Err(SolverError::DependencyViolation);
        }
        ends.push(end);
        let job_cost = duration
            .checked_mul(u64::from(job.units))
            .and_then(|units| units.checked_mul(machine.price_per_unit_tick))
            .ok_or(SolverError::ArithmeticOverflow)?;
        cost = cost
            .checked_add(job_cost)
            .ok_or(SolverError::ArithmeticOverflow)?;
    }
    for assignment in schedule {
        let mut occupied = 0u64;
        for (index, other) in schedule.iter().enumerate() {
            if other.machine == assignment.machine
                && other.start <= assignment.start
                && assignment.start < ends[index]
            {
                occupied = occupied
                    .checked_add(u64::from(instance.jobs[index].units))
                    .ok_or(SolverError::ArithmeticOverflow)?;
            }
        }
        if occupied > u64::from(instance.machines[assignment.machine as usize].capacity) {
            return Err(SolverError::CapacityExceeded);
        }
    }
    Ok(cost)
}

fn supported_rule(rule: &Rule) -> bool {
    match rule {
        Rule::All(children) | Rule::Any(children) => children.iter().all(supported_rule),
        Rule::Accepted
        | Rule::AmountAtMost(_)
        | Rule::DeliverableEquals(_)
        | Rule::RecipientEquals(_) => true,
        _ => false, // No registry, invoice, or caller-supplied acceptance facts exist here.
    }
}

pub fn authorize_solver(input: &SolverInput) -> Result<SolverAuthorization, SolverError> {
    let policy = &input.policy;
    let request = &input.request;
    if policy.version == 0
        || !valid_scope(&policy.scope)
        || policy.valid_after > policy.valid_until
        || policy.checker_version != CHECKER_VERSION
        || policy.instance_hash == [0; 32]
        || policy.max_cost == 0
        || !valid_rule(&policy.rule, false, 0, &mut 128)
        || !supported_rule(&policy.rule)
    {
        return Err(SolverError::InvalidPolicy);
    }
    if request.scope != policy.scope
        || request.amount == 0
        || request.task_id == [0; 32]
        || request.recipient == [0; 20]
        || request.deliverable_hash == [0; 32]
    {
        return Err(SolverError::InvalidRequest);
    }
    // These are mandatory gates, outside the rule tree. Any/omitted Accepted cannot bypass them.
    validate_instance(&input.instance)?;
    if instance_hash(&input.instance) != policy.instance_hash {
        return Err(SolverError::InstanceMismatch);
    }
    let total_cost = check_schedule(&input.instance, &input.schedule)?;
    if total_cost > policy.max_cost {
        return Err(SolverError::CostExceeded);
    }
    if result_hash(&input.schedule)? != request.deliverable_hash {
        return Err(SolverError::ResultMismatch);
    }
    let facts = Facts {
        amount: request.amount,
        category: 0,
        accepted: true,
        deliverable: request.deliverable_hash,
        recipient: request.recipient,
        line_labels: Vec::new(),
        no_denied_term: false,
        po_remaining: 0,
    };
    if !evaluate(&policy.rule, &facts) {
        return Err(SolverError::PolicyDenied);
    }
    Ok(SolverAuthorization {
        authorization: Authorization {
            policy_hash: policy_hash(policy),
            request: request.clone(),
            policy_version: policy.version,
            valid_after: policy.valid_after,
            valid_until: policy.valid_until,
            evidence_hash: hash_tagged(
                b"warrant/solver-evidence/v1",
                &(
                    CHECKER_VERSION,
                    policy.instance_hash,
                    request.deliverable_hash,
                    total_cost,
                ),
            ),
        },
        total_cost,
    })
}
