//! Per-user tavern drink tally backing the clubhouse drunkenness glow.
//!
//! `drunk_points` is the raw buzz recorded at `last_drink_at` (chips spent on
//! drinks, capped at [`MAX_DRUNK_POINTS`]). Nothing ever writes a sober-up:
//! readers apply [`decayed_points`] against elapsed wall-clock time, so a user
//! dries out on their own and the row only changes when they buy again.

use std::collections::HashMap;

use anyhow::Result;
use chrono::{DateTime, Utc};
use deadpool_postgres::GenericClient;
use tokio_postgres::{Client, GenericClient as TokioGenericClient};
use uuid::Uuid;

/// Bounds on what the bartender may charge for a single pour.
pub const DRINK_PRICE_MIN: i64 = 100;
pub const DRINK_PRICE_MAX: i64 = 1_000;
/// Buzz comped to a newcomer on their first walk up to the bar. Sized to land
/// exactly on the first drunk level so the welcome round already glows.
pub const WELCOME_DRINK_POINTS: i64 = 100;
/// Hard cap on stored points so one binge can't glow for days.
pub const MAX_DRUNK_POINTS: i64 = 4_000;
/// How long a patron sitting on the cap takes to come back to fully sober.
/// This is the dial to turn when the bar feels too forgiving or too punishing;
/// the per-hour rate below follows from it. Half a day means a big night is
/// gone by the next evening, and entering "wasted" (2000) clears in six hours.
pub const DRUNK_SOBER_UP_HOURS: i64 = 12;
/// How fast the buzz wears off, in drunk points (= chips) per hour. Derived
/// from the cap and [`DRUNK_SOBER_UP_HOURS`], rounded up so the last hour
/// finishes the job instead of leaving a point of buzz behind.
pub const DRUNK_DECAY_PER_HOUR: i64 =
    (MAX_DRUNK_POINTS + DRUNK_SOBER_UP_HOURS - 1) / DRUNK_SOBER_UP_HOURS;

/// Level thresholds on effective (decayed) points. Level 0 renders nothing;
/// level 1 ("tipsy", the welcome round) already earns its printed label;
/// level 4 ("fully wasted") lands at 2000, two top-shelf pours deep.
const DRUNK_LEVEL_THRESHOLDS: [i64; 4] = [1, 300, 1_000, 2_000];

/// Lowest level that earns a printed "(word)" label next to the name. Every
/// non-sober level gets one now that the label is the only drunk indicator.
pub const DRUNK_LABEL_MIN_LEVEL: u8 = 1;

/// The top drunk level ("wasted"). The bar keeps pouring the strong stuff right
/// up to here so a patron can actually climb the ladder; only once they hit it
/// does the bartender cut them off.
pub const DRUNK_MAX_LEVEL: u8 = DRUNK_LEVEL_THRESHOLDS.len() as u8;

/// The patron's state as a single word, for the bartender prompt and the
/// clubhouse name label. Level 0 is sober; 4 is fully wasted.
pub fn drunk_level_word(level: u8) -> &'static str {
    match level {
        0 => "sober",
        1 => "tipsy",
        2 => "buzzed",
        3 => "sloshed",
        _ => "wasted",
    }
}

/// The word shown beside a drinker's name, or `None` when they are too sober
/// (below [`DRUNK_LABEL_MIN_LEVEL`]) to warrant one.
pub fn drunk_label_word(level: u8) -> Option<&'static str> {
    (level >= DRUNK_LABEL_MIN_LEVEL).then(|| drunk_level_word(level))
}

/// Effective points after `elapsed_seconds` of sobering up.
pub fn decayed_points(points: i64, elapsed_seconds: i64) -> i64 {
    if points <= 0 {
        return 0;
    }
    let decay = elapsed_seconds.max(0) * DRUNK_DECAY_PER_HOUR / 3600;
    (points - decay).max(0)
}

/// Bucket effective points into a render level 0 (sober) through 4 (wasted).
pub fn drunk_level(effective_points: i64) -> u8 {
    DRUNK_LEVEL_THRESHOLDS
        .iter()
        .filter(|threshold| effective_points >= **threshold)
        .count() as u8
}
/// Minimum price in chips for the Hangover Cure item.
pub const HANGOVER_CURE_MIN_PRICE: i64 = 250;

/// Calculate the dynamic price for Hangover Cure from current effective drunk points,
/// lifetime drink spend (log-scaled multiplier for heavy drinkers), and random jitter (0.85 to 1.15).
/// If sober (0 points), returns 0.
pub fn hangover_cure_price(effective_points: i64, lifetime_spent: i64, seed: u64) -> i64 {
    if effective_points <= 0 {
        return 0;
    }
    // Lifetime tolerance/habituation multiplier: 1.0 + 0.18 * ln(1 + lifetime_spent / 1000)^2.15
    // e.g. 0 spent -> 1.0x, 1000 spent -> ~1.08x, 5000 spent -> ~1.63x, 20000 spent -> ~2.97x, 50000 spent -> ~4.42x, 100000 spent -> ~5.82x
    let ln_val = (1.0 + (lifetime_spent.max(0) as f64) / 1000.0).ln();
    let lifetime_mult = 1.0 + 0.18 * ln_val.powf(2.15);
    // Jitter: 85% to 115% (85..=115 / 100)
    let jitter_pct = 85 + (seed % 31) as i64;
    let base_with_lifetime = (effective_points as f64 * lifetime_mult).round() as i64;
    let raw_price = (base_with_lifetime * jitter_pct + 50) / 100;
    raw_price.max(HANGOVER_CURE_MIN_PRICE)
}

/// Generate a pseudo-random seed for pricing jitter using system entropy.
pub fn hangover_cure_seed() -> u64 {
    let mut bytes = [0u8; 8];
    let _ = getrandom::fill(&mut bytes);
    u64::from_le_bytes(bytes)
}
#[derive(Debug, Clone)]
pub struct UserDrinks {
    pub user_id: Uuid,
    pub drunk_points: i64,
    pub lifetime_spent: i64,
    pub drink_count: i64,
    pub last_drink_at: DateTime<Utc>,
}

impl From<tokio_postgres::Row> for UserDrinks {
    fn from(row: tokio_postgres::Row) -> Self {
        Self {
            user_id: row.get("user_id"),
            drunk_points: row.get("drunk_points"),
            lifetime_spent: row.get("lifetime_spent"),
            drink_count: row.get("drink_count"),
            last_drink_at: row.get("last_drink_at"),
        }
    }
}

impl UserDrinks {
    /// Points remaining right now, after sobering up since the last drink.
    pub fn effective_points(&self, now: DateTime<Utc>) -> i64 {
        decayed_points(self.drunk_points, (now - self.last_drink_at).num_seconds())
    }

    /// Render level 0-4 right now.
    pub fn level(&self, now: DateTime<Utc>) -> u8 {
        drunk_level(self.effective_points(now))
    }

    /// Record a paid drink: `price` chips become both buzz and tab.
    pub async fn record_purchase(
        client: &impl GenericClient,
        user_id: Uuid,
        price: i64,
    ) -> Result<Self> {
        Self::record_pour(client, user_id, price, price).await
    }

    /// Record a drink somebody else already paid for: the buzz lands, the tab
    /// does not. A round's credit is the only way to get one
    /// ([`crate::models::drink_round`]), and it pours a flat
    /// [`crate::models::drink_round::ROUND_DRINK_POINTS`] whatever the
    /// bartender named the drink, so what the house comps never depends on
    /// what he invented to call it. `lifetime_spent` stays put because the
    /// drinker spent nothing: the chips are on the buyer's ledger row.
    pub async fn record_comped_pour(
        client: &impl GenericClient,
        user_id: Uuid,
        points: i64,
    ) -> Result<Self> {
        Self::record_pour(client, user_id, points, 0).await
    }

    /// Decay the stored buzz to now, add `points`, cap it, and bump the
    /// tallies by `spent`. One statement, so concurrent buys from two
    /// sessions can't double-count the decay window. Every numeric parameter
    /// is cast to bigint so Postgres never infers a `LEAST`/`GREATEST`
    /// argument as text.
    async fn record_pour(
        client: &impl GenericClient,
        user_id: Uuid,
        points: i64,
        spent: i64,
    ) -> Result<Self> {
        let row = client
            .query_one(
                "INSERT INTO user_drinks
                    (user_id, drunk_points, lifetime_spent, drink_count, last_drink_at)
                 VALUES ($1, LEAST($2::bigint, $4::bigint), $5::bigint, 1, current_timestamp)
                 ON CONFLICT (user_id) DO UPDATE SET
                    drunk_points = LEAST(
                        GREATEST(
                            user_drinks.drunk_points
                                - (EXTRACT(EPOCH FROM (current_timestamp - user_drinks.last_drink_at))::bigint * $3::bigint / 3600),
                            0
                        ) + $2::bigint,
                        $4::bigint
                    ),
                    lifetime_spent = user_drinks.lifetime_spent + $5::bigint,
                    drink_count = user_drinks.drink_count + 1,
                    last_drink_at = current_timestamp,
                    updated = current_timestamp
                 RETURNING *",
                &[
                    &user_id,
                    &points,
                    &DRUNK_DECAY_PER_HOUR,
                    &MAX_DRUNK_POINTS,
                    &spent,
                ],
            )
            .await?;
        Ok(Self::from(row))
    }

    /// Comp the newcomer's welcome round: `points` of buzz with no chips
    /// charged, insert-only so it lands at most once per user ever. `None`
    /// when a `user_drinks` row already exists (they have drunk before, the
    /// welcome is spent), which lets the caller re-fire safely across
    /// sessions without double-comping.
    pub async fn record_welcome_pour(
        client: &impl GenericClient,
        user_id: Uuid,
        points: i64,
    ) -> Result<Option<Self>> {
        let row = client
            .query_opt(
                "INSERT INTO user_drinks
                    (user_id, drunk_points, lifetime_spent, drink_count, last_drink_at)
                 VALUES ($1, LEAST($2::bigint, $3::bigint), 0, 1, current_timestamp)
                 ON CONFLICT (user_id) DO NOTHING
                 RETURNING *",
                &[&user_id, &points, &MAX_DRUNK_POINTS],
            )
            .await?;
        Ok(row.map(Self::from))
    }
    /// Purge all drunk points for the user immediately (Hangover Cure).
    /// Resets `drunk_points` to 0 and updates `last_drink_at` to current_timestamp
    /// while preserving lifetime stats (`lifetime_spent` and `drink_count`).
    pub async fn purge_in_tx(client: &impl TokioGenericClient, user_id: Uuid) -> Result<()> {
        client
            .execute(
                "UPDATE user_drinks
                 SET drunk_points = 0,
                     last_drink_at = current_timestamp,
                     updated = current_timestamp
                 WHERE user_id = $1",
                &[&user_id],
            )
            .await?;
        Ok(())
    }

    pub async fn find(client: &Client, user_id: Uuid) -> Result<Option<Self>> {
        let row = client
            .query_opt("SELECT * FROM user_drinks WHERE user_id = $1", &[&user_id])
            .await?;
        Ok(row.map(Self::from))
    }
    pub async fn find_in_tx(client: &impl TokioGenericClient, user_id: Uuid) -> Result<Option<Self>> {
        let row = client
            .query_opt("SELECT * FROM user_drinks WHERE user_id = $1", &[&user_id])
            .await?;
        Ok(row.map(Self::from))
    }


    /// Rows that can still be drunk right now: anything that drank recently
    /// enough that the cap hasn't fully decayed (the window sits above
    /// [`DRUNK_SOBER_UP_HOURS`] with room to spare). Callers compute per-user
    /// levels from these with [`UserDrinks::level`].
    pub async fn all_active(client: &Client) -> Result<Vec<Self>> {
        let rows = client
            .query(
                "SELECT * FROM user_drinks
                 WHERE drunk_points > 0
                   AND last_drink_at > current_timestamp - interval '18 hours'",
                &[],
            )
            .await?;
        Ok(rows.into_iter().map(Self::from).collect())
    }

    /// Current drunk levels (only levels > 0) for all recently-drinking users.
    pub async fn active_levels(client: &Client, now: DateTime<Utc>) -> Result<HashMap<Uuid, u8>> {
        Ok(Self::all_active(client)
            .await?
            .into_iter()
            .filter_map(|drinks| {
                let level = drinks.level(now);
                (level > 0).then_some((drinks.user_id, level))
            })
            .collect())
    }
}
