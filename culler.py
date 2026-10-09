import argparse
import sys
import warnings

# Suppress known upstream third-party FutureWarning (e.g. Keras/TF np.object warning)
warnings.filterwarnings("ignore", category=FutureWarning, module="keras.*")
warnings.filterwarnings("ignore", message=".*np\\.object.*", category=FutureWarning)
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.prompt import Prompt

from culler import CullingSession, FlagState, ExifToolWrapper, DatabaseManager, resolve_input_path, find_item_index_by_path

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fast Image Culling Tool (Sony ARW, JPG, PNG, HEIC)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-w", "--workspace", default=None, help="Path to custom .qc-workspace database file")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Scan directory and report image metadata & statistics")
    scan_parser.add_argument("path", help="Path to directory or image file")
    scan_parser.add_argument("-r", "--recursive", action="store_true", help="Scan directory recursively")

    # Cull interactive command
    cull_parser = subparsers.add_parser("cull", help="Interactive terminal image culling session")
    cull_parser.add_argument("path", help="Path to directory or image file")

    # Move Picked command
    move_p_parser = subparsers.add_parser("move-picked", help="Move all picked (Flag = PICK) images to subfolder")
    move_p_parser.add_argument("path", help="Path to directory or image file")
    move_p_parser.add_argument("--target", default="_SELECTED", help="Subfolder name for picked images")

    # Move Rejected command
    move_r_parser = subparsers.add_parser("move-rejected", help="Move all rejected (Flag = REJECT) images to subfolder")
    move_r_parser.add_argument("path", help="Path to directory or image file")
    move_r_parser.add_argument("--target", default="_REJECTED", help="Subfolder name for rejected images")

    # Trash / Delete Rejected command
    trash_r_parser = subparsers.add_parser("trash-rejected", help="Move all rejected (Flag = REJECT) images to OS Recycle Bin / Trash")
    trash_r_parser.add_argument("path", help="Path to directory or image file")
    trash_r_parser.add_argument("-f", "--format", default=None, help="Optional format filter (e.g. JPG, RAW)")

    # Auto Cull Blurry command
    auto_parser = subparsers.add_parser("auto-blur", help="Automatically flag bottom %% blurriest images as REJECT")
    auto_parser.add_argument("path", help="Path to directory or image file")
    auto_parser.add_argument("-p", "--percentile", type=float, default=15.0, help="Bottom percentile threshold (default: 15%%)")

    # Auto Detect Duplicates command
    auto_dup_parser = subparsers.add_parser("auto-duplicate", help="Detect duplicate photos, keep best, flag rejects")
    auto_dup_parser.add_argument("path", help="Path to directory or image file")
    auto_dup_parser.add_argument("-m", "--method", choices=["dhash", "histogram"], default="dhash", help="Duplicate detection method")
    auto_dup_parser.add_argument("-t", "--threshold", type=float, default=6.0, help="Similarity threshold (default: 6.0)")
    auto_dup_parser.add_argument("--keeper", choices=["sharpest", "newest", "largest"], default="sharpest", help="Keeper selection strategy")

    # Export command
    export_parser = subparsers.add_parser("export", help="Export culling manifest and metadata to JSON or CSV")
    export_parser.add_argument("path", help="Path to directory or image file")
    export_parser.add_argument("-o", "--output", default="cull_manifest.json", help="Output file path")
    export_parser.add_argument("--csv", action="store_true", help="Export as CSV instead of JSON")

    # Sync EXIF command
    sync_parser = subparsers.add_parser("sync-exif", help="Write star ratings directly back to image EXIF/XMP via ExifTool")
    sync_parser.add_argument("path", help="Path to directory or image file")

    # GUI command
    gui_parser = subparsers.add_parser("gui", help="Launch Graphical User Interface")
    gui_parser.add_argument("path", nargs="?", default=None, help="Optional path to directory or image file")

    return parser


def cmd_scan(session: CullingSession, args):
    folder_path, target_image = resolve_input_path(args.path)
    console.print(f"[bold cyan]Scanning directory:[/bold cyan] {folder_path}")
    if target_image:
        console.print(f"[bold magenta]Target image specified:[/bold magenta] {target_image.name}")
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True
    ) as progress:
        task = progress.add_task("Reading EXIF metadata...", total=None)
        items = session.scan_directory(folder_path, recursive=args.recursive)

    if not items:
        console.print("[yellow]No supported images (ARW, JPG, PNG, HEIC) found in directory.[/yellow]")
        return

    stats = session.get_summary_stats()
    
    # Overview Panel
    console.print(Panel(
        f"[bold white]Total Images:[/bold white] [green]{stats['total_images']}[/green]  |  "
        f"[bold white]Total Size:[/bold white] [yellow]{stats['total_size_mb']} MB[/yellow]\n"
        f"[bold green]Picked:[/bold green] {stats['picked']}  |  "
        f"[bold red]Rejected:[/bold red] {stats['rejected']}  |  "
        f"[bold grey]Unflagged:[/bold grey] {stats['unflagged']}",
        title="[bold blue]Directory Summary[/bold blue]",
        expand=False
    ))

    # Format Table
    table = Table(title="File List & EXIF Metadata", show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4)
    table.add_column("Filename", style="bold white")
    table.add_column("Format", style="cyan")
    table.add_column("Size", style="yellow")
    table.add_column("Camera", style="green")
    table.add_column("Lens", style="blue")
    table.add_column("ISO", style="dim")
    table.add_column("Shutter", style="dim")
    table.add_column("Aperture", style="dim")
    table.add_column("Flag", style="bold")

    for idx, item in enumerate(items, 1):
        flag_str = "[green]PICK[/green]" if item.flag == FlagState.PICK else (
            "[red]REJECT[/red]" if item.flag == FlagState.REJECT else "[dim]UNFLAGGED[/dim]"
        )
        is_target = target_image and (item.path.resolve() == target_image.resolve() or any(sp.resolve() == target_image.resolve() for sp in item.stacked_paths))
        row_style = "bold yellow" if is_target else None
        table.add_row(
            str(idx),
            item.filename + (" (Target)" if is_target else ""),
            item.format_name,
            item.formatted_size,
            str(item.metadata.get("model", "N/A")),
            str(item.metadata.get("lens", "N/A")),
            str(item.metadata.get("iso", "N/A")),
            str(item.metadata.get("shutter_speed", "N/A")),
            str(item.metadata.get("aperture", "N/A")),
            flag_str,
            style=row_style
        )

    console.print(table)


def cmd_cull(session: CullingSession, args):
    folder_path, target_image = resolve_input_path(args.path)
    items = session.scan_directory(folder_path)
    if not items:
        console.print("[yellow]No supported images found.[/yellow]")
        return

    start_idx = 0
    if target_image:
        found_idx, _ = session.find_item(target_image)
        if found_idx >= 0:
            start_idx = found_idx
            console.print(f"[green]Starting interactive culling at target image #{start_idx + 1}: {target_image.name}[/green]")

    console.print(Panel("[bold green]Interactive Terminal Image Culler[/bold green]\n"
                        "Controls:\n"
                        "  [bold green]p[/bold green] = Pick | [bold green]P[/bold green] = UnPick | [bold red]x[/bold red] = Reject | [bold red]X[/bold red] = UnReject | [bold grey]u[/bold grey] = Unflag\n"
                        "  [bold yellow]1-5[/bold yellow] = Set Stars | [bold blue]n[/bold blue] = Next | [bold blue]b[/bold blue] = Back | [bold magenta]q[/bold magenta] = Quit"))

    idx = start_idx
    while idx < len(items):
        item = items[idx]
        console.rule(f"Image {idx + 1} of {len(items)}: {item.filename}")
        
        meta = item.metadata
        flag_display = f"[bold green]PICK[/bold green]" if item.flag == FlagState.PICK else (
            f"[bold red]REJECT[/bold red]" if item.flag == FlagState.REJECT else "[dim]UNFLAGGED[/dim]"
        )
        stars = "★" * item.rating + "☆" * (5 - item.rating)

        console.print(
            f"  [bold]Format:[/bold] {item.format_name} ({item.formatted_size})\n"
            f"  [bold]Camera:[/bold] {meta.get('model', 'N/A')} | [bold]Lens:[/bold] {meta.get('lens', 'N/A')}\n"
            f"  [bold]Settings:[/bold] ISO {meta.get('iso', 'N/A')}  |  {meta.get('shutter_speed', 'N/A')}  |  {meta.get('aperture', 'N/A')}  |  {meta.get('focal_length', 'N/A')}\n"
            f"  [bold]Current Flag:[/bold] {flag_display}  |  [bold]Rating:[/bold] [yellow]{stars}[/yellow]"
        )

        action = Prompt.ask(
            "Action",
            choices=["p", "P", "x", "X", "u", "1", "2", "3", "4", "5", "n", "b", "q"],
            default="n"
        )

        if action == "p":
            item.flag = FlagState.PICK
            console.print("[green]-> Flagged as PICK[/green]")
            idx += 1
        elif action == "P":
            if item.flag == FlagState.PICK:
                item.flag = FlagState.UNFLAGGED
                console.print("[grey]-> Unpicked (Flag reset to UNFLAGGED)[/grey]")
            else:
                console.print("[grey]-> Item was not PICK[/grey]")
        elif action == "x":
            item.flag = FlagState.REJECT
            console.print("[red]-> Flagged as REJECT[/red]")
            idx += 1
        elif action == "X":
            if item.flag == FlagState.REJECT:
                item.flag = FlagState.UNFLAGGED
                console.print("[grey]-> Unrejected (Flag reset to UNFLAGGED)[/grey]")
            else:
                console.print("[grey]-> Item was not REJECT[/grey]")
        elif action == "u":
            item.flag = FlagState.UNFLAGGED
            console.print("[grey]-> Unflagged[/grey]")
        elif action in ["1", "2", "3", "4", "5"]:
            item.rating = int(action)
            console.print(f"[yellow]-> Set rating to {action} stars[/yellow]")
        elif action == "n":
            idx += 1
        elif action == "b":
            idx = max(0, idx - 1)
        elif action == "q":
            break

    stats = session.get_summary_stats()
    console.print(f"\n[bold cyan]Culling Session Ended.[/bold cyan] Picked: [green]{stats['picked']}[/green], Rejected: [red]{stats['rejected']}[/red]")


def cmd_auto_blur(session: CullingSession, args):
    folder_path, _ = resolve_input_path(args.path)
    session.scan_directory(folder_path)
    console.print(f"[cyan]Calculating sharpness variance across {len(session.items)} images...[/cyan]")
    
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console
    ) as progress:
        task = progress.add_task("Analyzing...", total=len(session.items))
        
        def update_cb(done, total):
            progress.update(task, completed=done)

        flagged = session.auto_cull_blurry(bottom_percentile=args.percentile)

    console.print(f"[bold red]Auto-flagged {len(flagged)} blurry images as REJECT (Bottom {args.percentile}% sharpness).[/bold red]")
    for item in flagged:
        console.print(f"  - [red]{item.filename}[/red] (Sharpness score: {item.sharpness_score})")


def cmd_auto_duplicate(session: CullingSession, args):
    folder_path, _ = resolve_input_path(args.path)
    session.scan_directory(folder_path)
    console.print(f"[cyan]Scanning {len(session.items)} images for duplicates ({args.method}, threshold={args.threshold})...[/cyan]")
    
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console
    ) as progress:
        task = progress.add_task("Detecting duplicates...", total=len(session.items))
        
        def update_cb(done, total):
            progress.update(task, completed=done)

        flagged = session.scan_for_duplicates(
            method=args.method,
            threshold=args.threshold,
            flag_action="Reject",
            tag_action="Duplicate",
            keeper_flag="Pick",
            keeper_method=args.keeper,
            progress_callback=update_cb
        )

    console.print(f"[bold red]Flagged {len(flagged)} duplicate images as REJECT.[/bold red]")
    for item in flagged:
        console.print(f"  - [red]{item.filename}[/red]")


def cmd_move_picked(session: CullingSession, args):
    folder_path, _ = resolve_input_path(args.path)
    session.scan_directory(folder_path)
    moved = session.move_items_by_flag(FlagState.PICK, args.target)
    console.print(f"[bold green]Moved {len(moved)} Picked images to subfolder '{args.target}'.[/bold green]")


def cmd_move_rejected(session: CullingSession, args):
    folder_path, _ = resolve_input_path(args.path)
    session.scan_directory(folder_path)
    moved = session.move_items_by_flag(FlagState.REJECT, args.target)
    console.print(f"[bold red]Moved {len(moved)} Rejected images to subfolder '{args.target}'.[/bold red]")


def cmd_trash_rejected(session: CullingSession, args):
    folder_path, _ = resolve_input_path(args.path)
    session.scan_directory(folder_path)
    moved = session.trash_rejected_items(format_filter=args.format)
    console.print(f"[bold red]Moved {moved} Rejected file(s) to Recycle Bin / Trash.[/bold red]")


def cmd_export(session: CullingSession, args):
    folder_path, _ = resolve_input_path(args.path)
    session.scan_directory(folder_path)
    fmt = "csv" if args.csv else "json"
    out_file = session.export_manifest(args.output, format_type=fmt)
    console.print(f"[bold green]Exported manifest to {out_file}[/bold green]")


def cmd_sync_exif(session: CullingSession, args):
    folder_path, _ = resolve_input_path(args.path)
    session.scan_directory(folder_path)
    console.print("[cyan]Writing star ratings to EXIF via ExifTool...[/cyan]")
    count = session.sync_exif_ratings()
    console.print(f"[bold green]Successfully updated EXIF ratings on {count} images.[/bold green]")


def cmd_gui(args):
    # Route through the bootstrap launcher so the splash appears immediately.
    from bootstrap import launch_gui
    launch_gui(
        initial_path=args.path,
        workspace_path=getattr(args, "workspace", None),
    )


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "gui":
        cmd_gui(args)
        return

    exif = ExifToolWrapper()
    if not exif.is_available():
        console.print("[bold red]Warning: ExifTool binary not found at default location. Sony ARW previews may use fallback rawpy.[/bold red]")

    db = DatabaseManager(args.workspace) if getattr(args, "workspace", None) else None
    session = CullingSession(exif_wrapper=exif, db_manager=db)

    if args.command == "scan":
        cmd_scan(session, args)
    elif args.command == "cull":
        cmd_cull(session, args)
    elif args.command == "auto-blur":
        cmd_auto_blur(session, args)
    elif args.command == "auto-duplicate":
        cmd_auto_duplicate(session, args)
    elif args.command == "move-picked":
        cmd_move_picked(session, args)
    elif args.command == "move-rejected":
        cmd_move_rejected(session, args)
    elif args.command == "trash-rejected":
        cmd_trash_rejected(session, args)
    elif args.command == "export":
        cmd_export(session, args)
    elif args.command == "sync-exif":
        cmd_sync_exif(session, args)


if __name__ == "__main__":
    main()

