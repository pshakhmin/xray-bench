use std::collections::HashMap;
use std::env;
use std::error::Error;
use std::io::{self, ErrorKind};
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinSet;
use tokio::time::{sleep, timeout};

const IO_TIMEOUT: Duration = Duration::from_secs(30);
const BUF_SIZE: usize = 64 * 1024;
const MIB: u64 = 1024 * 1024;

const HELP: &str = "\
bench - SOCKS5 proxy throughput / setup benchmark

Usage:
  bench sink --listen <ip:port>
  bench run  [--proxy <socks ip:port> | --direct] --target <sink ip:port>
             [--mode down|up] [--mib N] [--conns C] [--runs R]
  bench ttfb [--proxy <socks ip:port> | --direct] --target <sink ip:port>
             [--runs R]
  bench conns [--proxy <socks ip:port> | --direct] --target <sink ip:port>
              [--count K] [--par P]

Commands:
  sink    Run the benchmark sink. Serves download (mode 0), upload (mode 1)
          and ping (mode 2) on every accepted TCP connection.
  run     Measure bulk throughput. All C connections are established before
          the timer starts. Payload is split evenly across connections.
  ttfb    Measure time from TCP connect through the SOCKS5 tunnel to the
          first payload byte of a ping connection.
  conns   Open K short-lived tunneled ping connections, at most P in flight.

Options:
  --direct   Skip SOCKS5 and connect straight to --target (self-test mode).
  --help     Show this help.

Notes:
  --proxy is required unless --direct is given.
  Target addresses must be IPv4 for SOCKS5 mode.
  Defaults: run mode=down mib=64 conns=1 runs=3;
            ttfb runs=10; conns count=100 par=10.
  Output on stdout is JSON lines only. Human-readable messages go to stderr.
";

#[derive(Clone, Copy)]
enum Mode {
    Down,
    Up,
}

impl Mode {
    fn as_str(self) -> &'static str {
        match self {
            Mode::Down => "down",
            Mode::Up => "up",
        }
    }
}

#[tokio::main]
async fn main() {
    if let Err(error) = dispatch().await {
        eprintln!("bench: {error}");
        std::process::exit(1);
    }
}

async fn dispatch() -> Result<(), Box<dyn Error>> {
    let mut args = env::args().skip(1);
    let Some(command) = args.next() else {
        print_help();
        return Err(invalid_input("missing subcommand"));
    };
    let rest: Vec<String> = args.collect();

    match command.as_str() {
        "--help" | "-h" | "help" => {
            print_help();
            Ok(())
        }
        "sink" => sink_cmd(&rest).await,
        "run" => run_cmd(&rest).await,
        "ttfb" => ttfb_cmd(&rest).await,
        "conns" => conns_cmd(&rest).await,
        other => Err(invalid_input(format!("unknown subcommand: {other}"))),
    }
}

fn print_help() {
    eprintln!("{HELP}");
}

fn invalid_input(message: impl Into<String>) -> Box<dyn Error> {
    Box::new(io::Error::new(ErrorKind::InvalidInput, message.into()))
}

fn timeout_error() -> io::Error {
    io::Error::new(ErrorKind::TimedOut, "operation timed out after 30s")
}

fn parse_flags(args: &[String]) -> Result<HashMap<String, String>, Box<dyn Error>> {
    let mut flags = HashMap::new();
    let mut index = 0;
    while index < args.len() {
        let argument = &args[index];
        let Some(key) = argument.strip_prefix("--") else {
            return Err(invalid_input(format!("unexpected argument: {argument}")));
        };
        if key.is_empty() {
            return Err(invalid_input("empty flag"));
        }
        if matches!(key, "help" | "direct") {
            flags.insert(key.to_string(), "true".to_string());
            index += 1;
            continue;
        }
        let Some(value) = args.get(index + 1) else {
            return Err(invalid_input(format!("missing value for {argument}")));
        };
        if value.starts_with("--") {
            return Err(invalid_input(format!("missing value for {argument}")));
        }
        flags.insert(key.to_string(), value.clone());
        index += 2;
    }
    Ok(flags)
}

fn required<'a>(
    flags: &'a HashMap<String, String>,
    key: &str,
) -> Result<&'a str, Box<dyn Error>> {
    match flags.get(key) {
        Some(value) => Ok(value.as_str()),
        None => Err(invalid_input(format!("missing --{key}"))),
    }
}

fn parse_u64(flags: &HashMap<String, String>, key: &str) -> Result<u64, Box<dyn Error>> {
    let value = required(flags, key)?;
    value
        .parse::<u64>()
        .map_err(|error| invalid_input(format!("invalid --{key} '{value}': {error}")))
}

fn parse_usize(flags: &HashMap<String, String>, key: &str) -> Result<usize, Box<dyn Error>> {
    let value = required(flags, key)?;
    value
        .parse::<usize>()
        .map_err(|error| invalid_input(format!("invalid --{key} '{value}': {error}")))
}

fn parse_addr(flags: &HashMap<String, String>, key: &str) -> Result<SocketAddr, Box<dyn Error>> {
    let value = required(flags, key)?;
    value
        .parse::<SocketAddr>()
        .map_err(|error| invalid_input(format!("invalid --{key} '{value}': {error}")))
}

fn parse_mode(flags: &HashMap<String, String>) -> Result<Mode, Box<dyn Error>> {
    match flags.get("mode").map(String::as_str).unwrap_or("down") {
        "down" => Ok(Mode::Down),
        "up" => Ok(Mode::Up),
        other => Err(invalid_input(format!(
            "invalid --mode '{other}' (expected down or up)"
        ))),
    }
}

fn is_direct(flags: &HashMap<String, String>) -> bool {
    flags.contains_key("direct")
}

fn optional_proxy(flags: &HashMap<String, String>) -> Result<Option<SocketAddr>, Box<dyn Error>> {
    match flags.get("proxy") {
        Some(_) => Ok(Some(parse_addr(flags, "proxy")?)),
        None => Ok(None),
    }
}

async fn read_exact_timeout<R>(reader: &mut R, buffer: &mut [u8]) -> io::Result<()>
where
    R: AsyncRead + Unpin,
{
    match timeout(IO_TIMEOUT, reader.read_exact(buffer)).await {
        Ok(Ok(_)) => Ok(()),
        Ok(Err(error)) => Err(error),
        Err(_) => Err(timeout_error()),
    }
}

async fn write_all_timeout<W>(writer: &mut W, buffer: &[u8]) -> io::Result<()>
where
    W: AsyncWrite + Unpin,
{
    match timeout(IO_TIMEOUT, writer.write_all(buffer)).await {
        Ok(result) => result,
        Err(_) => Err(timeout_error()),
    }
}

async fn shutdown_timeout<W>(writer: &mut W) -> io::Result<()>
where
    W: AsyncWrite + Unpin,
{
    match timeout(IO_TIMEOUT, writer.shutdown()).await {
        Ok(result) => result,
        Err(_) => Err(timeout_error()),
    }
}

async fn connect_timeout(address: SocketAddr) -> io::Result<TcpStream> {
    let stream = timeout(IO_TIMEOUT, TcpStream::connect(address))
        .await
        .map_err(|_| timeout_error())??;
    stream.set_nodelay(true)?;
    Ok(stream)
}

fn fill_payload(buffer: &mut [u8], offset: u64) {
    for (index, byte) in buffer.iter_mut().enumerate() {
        *byte = (offset.wrapping_add(index as u64) & 0xff) as u8;
    }
}

// ---------------------------------------------------------------------------
// sink
// ---------------------------------------------------------------------------

async fn sink_cmd(args: &[String]) -> Result<(), Box<dyn Error>> {
    let flags = parse_flags(args)?;
    if flags.contains_key("help") {
        print_help();
        return Ok(());
    }
    let listen = parse_addr(&flags, "listen")?;
    let listener = TcpListener::bind(listen)
        .await
        .map_err(|error| invalid_input(format!("cannot bind {listen}: {error}")))?;
    eprintln!("sink listening on {listen}");

    loop {
        match listener.accept().await {
            Ok((socket, _peer)) => {
                tokio::spawn(async move {
                    let _ = sink_connection(socket).await;
                });
            }
            Err(error) => {
                eprintln!("sink accept error: {error}");
                sleep(Duration::from_millis(100)).await;
            }
        }
    }
}

async fn sink_connection(mut socket: TcpStream) -> io::Result<()> {
    socket.set_nodelay(true)?;
    let mut buffer = vec![0u8; BUF_SIZE];
    let mut mode = [0u8; 1];
    read_exact_timeout(&mut socket, &mut mode).await?;

    match mode[0] {
        0 => {
            // Advertise a very large download so clients can stop after their
            // own share and close. The payload is generated on the fly.
            let advertised = u32::MAX;
            write_all_timeout(&mut socket, &advertised.to_be_bytes()).await?;
            let mut remaining = u64::from(advertised);
            let mut offset = 0u64;
            while remaining > 0 {
                let take = remaining.min(BUF_SIZE as u64) as usize;
                fill_payload(&mut buffer[..take], offset);
                write_all_timeout(&mut socket, &buffer[..take]).await?;
                remaining -= take as u64;
                offset = offset.wrapping_add(take as u64);
            }
            shutdown_timeout(&mut socket).await
        }
        1 => {
            let mut header = [0u8; 4];
            read_exact_timeout(&mut socket, &mut header).await?;
            let mut remaining = u64::from(u32::from_be_bytes(header));
            while remaining > 0 {
                let take = remaining.min(BUF_SIZE as u64) as usize;
                read_exact_timeout(&mut socket, &mut buffer[..take]).await?;
                remaining -= take as u64;
            }
            write_all_timeout(&mut socket, &[0x01]).await?;
            shutdown_timeout(&mut socket).await
        }
        2 => {
            write_all_timeout(&mut socket, &[0x01]).await?;
            // Keep the connection open until the client hangs up. A read
            // timeout also bounds a misbehaving peer.
            let mut scratch = [0u8; 1];
            loop {
                match timeout(IO_TIMEOUT, socket.read(&mut scratch)).await {
                    Ok(Ok(0)) | Ok(Err(_)) | Err(_) => break,
                    Ok(Ok(_)) => {}
                }
            }
            Ok(())
        }
        other => Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("unknown sink mode {other}"),
        )),
    }
}

// ---------------------------------------------------------------------------
// SOCKS5 / connection helpers
// ---------------------------------------------------------------------------

async fn connect_target(
    proxy: Option<SocketAddr>,
    target: SocketAddr,
    direct: bool,
) -> io::Result<TcpStream> {
    if direct {
        return connect_timeout(target).await;
    }
    let proxy = proxy.ok_or_else(|| {
        io::Error::new(ErrorKind::InvalidInput, "missing --proxy (or use --direct)")
    })?;
    socks5_connect(proxy, target).await
}

async fn socks5_connect(proxy: SocketAddr, target: SocketAddr) -> io::Result<TcpStream> {
    let mut socket = connect_timeout(proxy).await?;

    write_all_timeout(&mut socket, &[0x05, 0x01, 0x00]).await?;
    let mut greeting = [0u8; 2];
    read_exact_timeout(&mut socket, &mut greeting).await?;
    if greeting != [0x05, 0x00] {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("unexpected SOCKS5 greeting reply: {greeting:02x?}"),
        ));
    }

    let IpAddr::V4(target_ip) = target.ip() else {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            "SOCKS5 mode only supports IPv4 targets",
        ));
    };

    let mut request = [0u8; 10];
    request[..4].copy_from_slice(&[0x05, 0x01, 0x00, 0x01]);
    request[4..8].copy_from_slice(&target_ip.octets());
    request[8..10].copy_from_slice(&target.port().to_be_bytes());
    write_all_timeout(&mut socket, &request).await?;

    let mut head = [0u8; 4];
    read_exact_timeout(&mut socket, &mut head).await?;
    if head[0] != 0x05 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("unexpected SOCKS5 reply version: {}", head[0]),
        ));
    }
    if head[1] != 0x00 {
        return Err(io::Error::new(
            ErrorKind::ConnectionRefused,
            format!("SOCKS5 CONNECT failed with reply {}", head[1]),
        ));
    }

    // Consume the complete bound address and port.
    match head[3] {
        0x01 => {
            let mut rest = [0u8; 6];
            read_exact_timeout(&mut socket, &mut rest).await?;
        }
        0x03 => {
            let mut length = [0u8; 1];
            read_exact_timeout(&mut socket, &mut length).await?;
            let mut rest = vec![0u8; usize::from(length[0]) + 2];
            read_exact_timeout(&mut socket, &mut rest).await?;
        }
        0x04 => {
            let mut rest = [0u8; 18];
            read_exact_timeout(&mut socket, &mut rest).await?;
        }
        other => {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                format!("unsupported SOCKS5 ATYP {other}"),
            ));
        }
    }

    Ok(socket)
}

// ---------------------------------------------------------------------------
// bulk run
// ---------------------------------------------------------------------------

async fn run_cmd(args: &[String]) -> Result<(), Box<dyn Error>> {
    let flags = parse_flags(args)?;
    if flags.contains_key("help") {
        print_help();
        return Ok(());
    }

    let target = parse_addr(&flags, "target")?;
    let direct = is_direct(&flags);
    let proxy = optional_proxy(&flags)?;
    if !direct && proxy.is_none() {
        return Err(invalid_input("--proxy is required unless --direct is used"));
    }

    let mode = parse_mode(&flags)?;
    let mib = match flags.get("mib") {
        Some(_) => parse_u64(&flags, "mib")?,
        None => 64,
    };
    let conns = match flags.get("conns") {
        Some(_) => parse_usize(&flags, "conns")?,
        None => 1,
    };
    let runs = match flags.get("runs") {
        Some(_) => parse_usize(&flags, "runs")?,
        None => 3,
    };
    if conns == 0 {
        return Err(invalid_input("--conns must be at least 1"));
    }
    if runs == 0 {
        return Err(invalid_input("--runs must be at least 1"));
    }

    let total_bytes = mib
        .checked_mul(MIB)
        .ok_or_else(|| invalid_input("--mib is too large"))?;

    let mut samples = Vec::with_capacity(runs);

    for run in 1..=runs {
        let (streams, setup_errors) =
            establish_connections(proxy, target, direct, conns).await;
        if streams.is_empty() {
            eprintln!(
                "run {run}: all {conns} connections failed during setup ({setup_errors} errors)"
            );
            return Err(invalid_input("too many connection failures"));
        }

        let actual = streams.len();
        let base = total_bytes / actual as u64;
        let remainder = total_bytes % actual as u64;
        let largest_share = base + u64::from(remainder > 0);
        if largest_share > u64::from(u32::MAX) {
            return Err(invalid_input(format!(
                "share of {largest_share} bytes exceeds the 4-byte length field"
            )));
        }

        let started = Instant::now();
        let mut tasks = JoinSet::new();
        for (index, stream) in streams.into_iter().enumerate() {
            let share = base + u64::from((index as u64) < remainder);
            tasks.spawn(async move { transfer_connection(stream, mode, share).await });
        }

        let mut transfer_errors = 0usize;
        while let Some(result) = tasks.join_next().await {
            match result {
                Ok(Ok(())) => {}
                Ok(Err(error)) => {
                    transfer_errors += 1;
                    eprintln!("run {run}: transfer error: {error}");
                }
                Err(error) => {
                    transfer_errors += 1;
                    eprintln!("run {run}: transfer task error: {error}");
                }
            }
        }
        let seconds = started.elapsed().as_secs_f64();
        let mbps = if seconds > 0.0 {
            total_bytes as f64 / 1e6 / seconds
        } else {
            0.0
        };
        println!(
            "{{\"run\":{run},\"mib\":{mib},\"conns\":{conns},\"seconds\":{seconds:.6},\"mbps\":{mbps:.3}}}"
        );
        samples.push(mbps);

        let failed = setup_errors + transfer_errors;
        if failed * 10 > conns {
            return Err(invalid_input(format!(
                "run {run}: {failed} of {conns} connections failed (>10%)"
            )));
        }
    }

    if samples.is_empty() {
        return Err(invalid_input("no successful runs"));
    }
    let median = median_of(&samples);
    let min = samples.iter().copied().fold(f64::INFINITY, f64::min);
    let max = samples.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    println!(
        "{{\"event\":\"summary\",\"mode\":\"{}\",\"median_mbps\":{median:.3},\"min_mbps\":{min:.3},\"max_mbps\":{max:.3}}}",
        mode.as_str()
    );

    Ok(())
}

async fn establish_connections(
    proxy: Option<SocketAddr>,
    target: SocketAddr,
    direct: bool,
    count: usize,
) -> (Vec<TcpStream>, usize) {
    let mut tasks = JoinSet::new();
    for index in 0..count {
        tasks.spawn(async move {
            let result = connect_target(proxy, target, direct).await;
            (index, result)
        });
    }

    let mut streams = Vec::with_capacity(count);
    let mut errors = 0usize;
    while let Some(result) = tasks.join_next().await {
        match result {
            Ok((_index, Ok(stream))) => streams.push(stream),
            Ok((index, Err(error))) => {
                errors += 1;
                eprintln!("connection {index} setup failed: {error}");
            }
            Err(error) => {
                errors += 1;
                eprintln!("connection task failed: {error}");
            }
        }
    }
    (streams, errors)
}

async fn transfer_connection(
    mut socket: TcpStream,
    mode: Mode,
    bytes: u64,
) -> io::Result<()> {
    match mode {
        Mode::Down => download_connection(&mut socket, bytes).await?,
        Mode::Up => upload_connection(&mut socket, bytes).await?,
    }
    shutdown_timeout(&mut socket).await
}

async fn download_connection(socket: &mut TcpStream, bytes: u64) -> io::Result<()> {
    write_all_timeout(socket, &[0x00]).await?;
    let mut header = [0u8; 4];
    read_exact_timeout(socket, &mut header).await?;
    let advertised = u64::from(u32::from_be_bytes(header));
    if advertised < bytes {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("sink advertised {advertised} bytes, need {bytes}"),
        ));
    }

    let mut buffer = vec![0u8; BUF_SIZE];
    let mut remaining = bytes;
    while remaining > 0 {
        let take = remaining.min(BUF_SIZE as u64) as usize;
        read_exact_timeout(socket, &mut buffer[..take]).await?;
        remaining -= take as u64;
    }
    Ok(())
}

async fn upload_connection(socket: &mut TcpStream, bytes: u64) -> io::Result<()> {
    let length = u32::try_from(bytes).map_err(|_| {
        io::Error::new(
            ErrorKind::InvalidInput,
            "upload share does not fit in a 4-byte length field",
        )
    })?;
    write_all_timeout(socket, &[0x01]).await?;
    write_all_timeout(socket, &length.to_be_bytes()).await?;

    let mut buffer = vec![0u8; BUF_SIZE];
    let mut remaining = bytes;
    let mut offset = 0u64;
    while remaining > 0 {
        let take = remaining.min(BUF_SIZE as u64) as usize;
        fill_payload(&mut buffer[..take], offset);
        write_all_timeout(socket, &buffer[..take]).await?;
        remaining -= take as u64;
        offset = offset.wrapping_add(take as u64);
    }

    let mut reply = [0u8; 1];
    read_exact_timeout(socket, &mut reply).await?;
    if reply[0] != 0x01 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("unexpected upload reply: {:#04x}", reply[0]),
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// TTFB
// ---------------------------------------------------------------------------

async fn ttfb_cmd(args: &[String]) -> Result<(), Box<dyn Error>> {
    let flags = parse_flags(args)?;
    if flags.contains_key("help") {
        print_help();
        return Ok(());
    }

    let target = parse_addr(&flags, "target")?;
    let direct = is_direct(&flags);
    let proxy = optional_proxy(&flags)?;
    if !direct && proxy.is_none() {
        return Err(invalid_input("--proxy is required unless --direct is used"));
    }
    let runs = match flags.get("runs") {
        Some(_) => parse_usize(&flags, "runs")?,
        None => 10,
    };
    if runs == 0 {
        return Err(invalid_input("--runs must be at least 1"));
    }

    let mut samples = Vec::with_capacity(runs);
    let mut errors = 0usize;
    for run in 1..=runs {
        match ttfb_once(proxy, target, direct).await {
            Ok(ms) => {
                println!("{{\"run\":{run},\"ms\":{ms:.3}}}");
                samples.push(ms);
            }
            Err(error) => {
                errors += 1;
                eprintln!("ttfb run {run} failed: {error}");
            }
        }
    }

    if samples.is_empty() {
        return Err(invalid_input("all TTFB runs failed"));
    }
    let median = median_of(&samples);
    let p95 = percentile_of(&samples, 95.0);
    println!(
        "{{\"event\":\"summary\",\"median_ms\":{median:.3},\"p95_ms\":{p95:.3},\"runs\":{runs},\"errors\":{errors}}}"
    );
    if errors * 10 > runs {
        return Err(invalid_input(format!(
            "{errors} of {runs} TTFB connections failed (>10%)"
        )));
    }
    Ok(())
}

async fn ttfb_once(
    proxy: Option<SocketAddr>,
    target: SocketAddr,
    direct: bool,
) -> io::Result<f64> {
    let started = Instant::now();
    let mut socket = connect_target(proxy, target, direct).await?;
    write_all_timeout(&mut socket, &[0x02]).await?;
    let mut reply = [0u8; 1];
    read_exact_timeout(&mut socket, &mut reply).await?;
    if reply[0] != 0x01 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("unexpected ping reply: {:#04x}", reply[0]),
        ));
    }
    let ms = started.elapsed().as_secs_f64() * 1000.0;
    shutdown_timeout(&mut socket).await?;
    Ok(ms)
}

// ---------------------------------------------------------------------------
// connection-rate test
// ---------------------------------------------------------------------------

async fn conns_cmd(args: &[String]) -> Result<(), Box<dyn Error>> {
    let flags = parse_flags(args)?;
    if flags.contains_key("help") {
        print_help();
        return Ok(());
    }

    let target = parse_addr(&flags, "target")?;
    let direct = is_direct(&flags);
    let proxy = optional_proxy(&flags)?;
    if !direct && proxy.is_none() {
        return Err(invalid_input("--proxy is required unless --direct is used"));
    }
    let count = match flags.get("count") {
        Some(_) => parse_usize(&flags, "count")?,
        None => 100,
    };
    let par = match flags.get("par") {
        Some(_) => parse_usize(&flags, "par")?,
        None => 10,
    };
    if count == 0 {
        return Err(invalid_input("--count must be at least 1"));
    }
    if par == 0 {
        return Err(invalid_input("--par must be at least 1"));
    }

    let next = Arc::new(AtomicUsize::new(0));
    let succeeded = Arc::new(AtomicUsize::new(0));
    let errors = Arc::new(AtomicUsize::new(0));

    let started = Instant::now();
    let mut workers = JoinSet::new();
    for _worker in 0..par {
        let next = Arc::clone(&next);
        let succeeded = Arc::clone(&succeeded);
        let errors = Arc::clone(&errors);
        workers.spawn(async move {
            loop {
                let index = next.fetch_add(1, Ordering::Relaxed);
                if index >= count {
                    break;
                }
                match ping_once(proxy, target, direct).await {
                    Ok(()) => {
                        succeeded.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(error) => {
                        errors.fetch_add(1, Ordering::Relaxed);
                        eprintln!("connection {index} failed: {error}");
                    }
                }
            }
        });
    }

    while let Some(result) = workers.join_next().await {
        if let Err(error) = result {
            errors.fetch_add(1, Ordering::Relaxed);
            eprintln!("worker task failed: {error}");
        }
    }
    let seconds = started.elapsed().as_secs_f64();
    let success_count = succeeded.load(Ordering::Relaxed);
    let error_count = errors.load(Ordering::Relaxed);
    let per_second = if seconds > 0.0 {
        success_count as f64 / seconds
    } else {
        0.0
    };
    println!(
        "{{\"event\":\"conns\",\"count\":{count},\"par\":{par},\"success\":{success_count},\"errors\":{error_count},\"seconds\":{seconds:.6},\"conns_per_sec\":{per_second:.3}}}"
    );

    if error_count * 10 > count {
        return Err(invalid_input(format!(
            "{error_count} of {count} connections failed (>10%)"
        )));
    }
    Ok(())
}

async fn ping_once(
    proxy: Option<SocketAddr>,
    target: SocketAddr,
    direct: bool,
) -> io::Result<()> {
    let mut socket = connect_target(proxy, target, direct).await?;
    write_all_timeout(&mut socket, &[0x02]).await?;
    let mut reply = [0u8; 1];
    read_exact_timeout(&mut socket, &mut reply).await?;
    if reply[0] != 0x01 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            format!("unexpected ping reply: {:#04x}", reply[0]),
        ));
    }
    shutdown_timeout(&mut socket).await
}

// ---------------------------------------------------------------------------
// statistics
// ---------------------------------------------------------------------------

fn median_of(values: &[f64]) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let length = sorted.len();
    if length % 2 == 1 {
        sorted[length / 2]
    } else {
        (sorted[length / 2 - 1] + sorted[length / 2]) / 2.0
    }
}

fn percentile_of(values: &[f64], percentile: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let length = sorted.len();
    let rank = (percentile / 100.0 * length as f64).ceil() as usize;
    let index = rank.saturating_sub(1).min(length - 1);
    sorted[index]
}
