-- Brush.app wrapper: double-click to pick a splat file, or drop a .ply onto the icon.
-- The real renderer is Contents/Resources/brush_app (Brush v0.3.0, arm64).

property brushRelPath : "Contents/Resources/brush_app"

on run
	set brushBin to my brushPath()
	try
		set theFile to choose file with prompt "选择要查看的高斯泼溅文件（.ply / .compressed.ply / .zip）" of type {"ply", "zip", "splat", "ksplat"}
		my openSplat(brushBin, POSIX path of theFile)
	on error errMsg number errNum
		if errNum is -128 then
			-- user cancelled: open the empty viewer so a file can be dropped on it
			my openSplat(brushBin, "")
		else
			display dialog "启动 Brush 失败：" & errMsg buttons {"好"} default button 1 with icon caution
		end if
	end try
end run

on open theFiles
	set brushBin to my brushPath()
	repeat with f in theFiles
		my openSplat(brushBin, POSIX path of f)
	end repeat
end open

on brushPath()
	return (POSIX path of (path to me)) & brushRelPath
end brushPath

on openSplat(brushBin, filePath)
	if filePath is "" then
		do shell script "nohup " & quoted form of brushBin & " --with-viewer >/dev/null 2>&1 &"
	else
		-- run from the file's own folder so Brush exports land next to the source
		set dirPath to do shell script "dirname " & quoted form of filePath
		do shell script "cd " & quoted form of dirPath & " && nohup " & quoted form of brushBin & " " & quoted form of filePath & " --with-viewer >/dev/null 2>&1 &"
	end if
end openSplat
