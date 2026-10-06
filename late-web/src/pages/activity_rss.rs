use crate::{AppState, error::AppError};
use axum::{
    Router,
    body::Body,
    extract::State,
    http::{
        HeaderValue, StatusCode,
        header::{CACHE_CONTROL, CONTENT_TYPE},
    },
    response::Response,
    routing::get,
};
use late_core::models::{chat_message::ChatMessage, chat_room::ChatRoom, user::User};
use std::collections::HashSet;

const FEED_URL: &str = "https://late.sh/activity.rss";
const SITE_URL: &str = "https://late.sh";
const SYSTEM_USERNAME: &str = "system";
const SYSTEM_LINE_PREFIX: &str = "· ";
const MAX_ITEMS: i64 = 50;

#[derive(Clone)]
struct FeedItem {
    id: String,
    title: String,
    pub_date: String,
}

pub(crate) fn router() -> Router<AppState> {
    Router::new().route("/activity.rss", get(activity_rss_handler))
}

async fn activity_rss_handler(State(state): State<AppState>) -> Result<Response, AppError> {
    let body = render_activity_feed(fetch_activity_items(state).await?);
    let mut response = Response::new(Body::from(body));
    *response.status_mut() = StatusCode::OK;
    response.headers_mut().insert(
        CONTENT_TYPE,
        HeaderValue::from_static("application/rss+xml; charset=utf-8"),
    );
    response.headers_mut().insert(
        CACHE_CONTROL,
        HeaderValue::from_static("no-store, no-cache, must-revalidate"),
    );
    Ok(response)
}

async fn fetch_activity_items(state: AppState) -> Result<Vec<FeedItem>, AppError> {
    let client = state.db.get().await?;
    let Some(lounge_room) = ChatRoom::find_lounge(&client).await? else {
        return Ok(Vec::new());
    };
    let messages = ChatMessage::list_recent(&client, lounge_room.id, MAX_ITEMS).await?;
    if messages.is_empty() {
        return Ok(Vec::new());
    }

    let user_ids: Vec<_> = messages
        .iter()
        .map(|message| message.user_id)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    let usernames = User::list_usernames_by_ids(&client, &user_ids).await?;

    let items = messages
        .into_iter()
        .filter_map(|message| {
            let username = usernames.get(&message.user_id)?;
            if !is_system_username(username) {
                return None;
            }
            let title = parse_activity_title(&message.body)?;
            Some(FeedItem {
                id: message.id.to_string(),
                title,
                pub_date: message.created.to_rfc2822(),
            })
        })
        .collect();

    Ok(items)
}

fn is_system_username(username: &str) -> bool {
    username.trim().eq_ignore_ascii_case(SYSTEM_USERNAME)
}

fn parse_activity_title(body: &str) -> Option<String> {
    body.strip_prefix(SYSTEM_LINE_PREFIX)
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(ToString::to_string)
}

fn render_activity_feed(items: Vec<FeedItem>) -> String {
    let mut feed = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>late.sh activity</title>
    <link>{SITE_URL}</link>
    <description>Recent activity events from late.sh users</description>
    <language>en-us</language>
    <atom:link href="{FEED_URL}" rel="self" type="application/rss+xml" xmlns:atom="http://www.w3.org/2005/Atom"/>
"#
    );

    if let Some(first) = items.first() {
        feed.push_str("    <lastBuildDate>");
        feed.push_str(&escape_xml(&first.pub_date));
        feed.push_str("</lastBuildDate>\n");
    }

    for item in items {
        let escaped_title = escape_xml(&item.title);
        let guid = format!("{SITE_URL}/activity/{}", item.id);
        feed.push_str("    <item>\n");
        feed.push_str("      <title>");
        feed.push_str(&escaped_title);
        feed.push_str("</title>\n");
        feed.push_str("      <description>");
        feed.push_str(&escaped_title);
        feed.push_str("</description>\n");
        feed.push_str("      <link>");
        feed.push_str(SITE_URL);
        feed.push_str("</link>\n");
        feed.push_str("      <guid isPermaLink=\"false\">");
        feed.push_str(&escape_xml(&guid));
        feed.push_str("</guid>\n");
        feed.push_str("      <pubDate>");
        feed.push_str(&escape_xml(&item.pub_date));
        feed.push_str("</pubDate>\n");
        feed.push_str("    </item>\n");
    }

    feed.push_str("  </channel>\n</rss>\n");
    feed
}

fn escape_xml(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

#[cfg(test)]
mod activity_rss_test {
    use super::{FEED_URL, FeedItem, parse_activity_title, render_activity_feed};

    #[test]
    fn parse_activity_title_strips_prefix() {
        let title = parse_activity_title("· mira joined").expect("activity title");
        assert_eq!(title, "mira joined");
    }

    #[test]
    fn parse_activity_title_rejects_non_activity_lines() {
        assert!(parse_activity_title("hello").is_none());
        assert!(parse_activity_title("· ").is_none());
    }

    #[test]
    fn render_activity_feed_escapes_xml() {
        let feed = render_activity_feed(vec![FeedItem {
            id: "00000000-0000-0000-0000-000000000000".to_string(),
            title: "mira & <kai> \"joined\"".to_string(),
            pub_date: "Mon, 01 Jan 2024 00:00:00 +0000".to_string(),
        }]);

        assert!(feed.contains(FEED_URL));
        assert!(feed.contains("mira &amp; &lt;kai&gt; &quot;joined&quot;"));
        assert!(feed.contains(
            "<guid isPermaLink=\"false\">https://late.sh/activity/00000000-0000-0000-0000-000000000000</guid>"
        ));
    }
}
