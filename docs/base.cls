% 作者：北京语言大学智能语音习得实验室，罗晓阳
% Author: BLCU-SAIT Xiaoyang Luo
% 2025/11/20
% !TEX program = xelatex

% ====================================================
% 1. 加载基类
% ====================================================
\LoadClass[a4paper,openany,oneside,zihao=-4]{ctexbook}

% ====================================================
% 2. 核心宏包
% ====================================================
\RequirePackage{geometry}
\RequirePackage{fancyhdr}
\RequirePackage{titletoc}
\RequirePackage[titles]{tocloft} 
\RequirePackage{graphicx}
\RequirePackage{amsmath}
\RequirePackage{exscale}
% --- 为公式编号设置字体 ---
\makeatletter
\renewcommand{\maketag@@@}[1]{\hbox{\m@th\normalsize\zihao{4}#1}}
\makeatother
% --------------------------------
\RequirePackage{float}
\RequirePackage{url}
\RequirePackage[numbers,sort&compress,square,super]{natbib}
\RequirePackage{etoolbox}
\RequirePackage{fontspec}
\RequirePackage{lastpage}
\RequirePackage[hidelinks]{hyperref} 
\RequirePackage{caption} 
\RequirePackage{booktabs} 
\RequirePackage{multirow}
\RequirePackage{setspace}  % 用于行间距设置

% ====================================================
% 3. 页面设置 (A4纸张，页边距)
% ====================================================
\geometry{
    left=3.17cm,
    right=3.17cm,
    top=2.54cm,
    bottom=2.54cm,
    headheight=0.5cm,
    headsep=0.54cm,
    footskip=0.79cm
}

% ====================================================
% 4. 字体设置
% ====================================================
% 西文字体：Times New Roman
\setmainfont{Times New Roman}[
  Path = ./,
  Extension = .ttf,
  UprightFont    = TimesNewRoman, % 对应 TimesNewRoman.ttf
  BoldFont       = timesbd,       % 对应 timesbd.ttf (粗体)
]

% 中文字体：宋体、黑体、楷体
\setCJKmainfont[Path=./, AutoFakeBold=true, AutoFakeSlant=true]{SimSun.ttf}
\setCJKfamilyfont{song}[Path=./, AutoFakeBold=true]{SimSun.ttf}
\setCJKfamilyfont{heiti}[Path=./, AutoFakeBold=true]{SimHei.ttf}
\setCJKfamilyfont{kaishu}[Path=./, AutoFakeBold=true]{KaiTi_GB2312.ttf}

\newcommand{\song}{\CJKfamily{song}}
\newcommand{\hei}{\CJKfamily{heiti}}
\newcommand{\kaishu}{\CJKfamily{kaishu}}

% ====================================================
% 5. 信息录入命令
% ====================================================
\makeatletter
\renewcommand{\title}[1]{\def\@title{#1}}
\newcommand{\titlesecondline}[1]{\def\@titlesecondline{#1}}
\def\@titlesecondline{}
\newcommand{\entitle}[1]{\def\@entitle{#1}}
\renewcommand{\author}[1]{\def\@author{#1}}
\newcommand{\enauthor}[1]{\def\@enauthor{#1}}
\newcommand{\stunum}[1]{\def\@stunum{#1}}
\newcommand{\grade}[1]{\def\@grade{#1}}
\newcommand{\college}[1]{\def\@college{#1}}
\newcommand{\encollege}[1]{\def\@encollege{#1}}
\newcommand{\major}[1]{\def\@major{#1}}
\newcommand{\instructor}[1]{\def\@instructor{#1}}
\newcommand{\eninstructor}[1]{\def\@eninstructor{#1}}
\newcommand{\degreetitle}[1]{\def\@degreetitle{#1}}
\renewcommand{\date}[3]{\def\@chineseyear{#1}\def\@chinesemonth{#2}\def\@arabicyear{#3}}
\newcommand{\datearabic}[2]{\def\@yeararabic{#1}\def\@montharabic{#2}}
\newcommand{\dateenglish}[2]{\def\@monthenglish{#1}\def\@yearenglish{#2}}

% ====================================================
% 6. 封面制作 (外封、中文内封、英文内封、声明)
% ====================================================
\renewcommand{\maketitle}{
% --- 封面 1: 外封 ---
    \begin{titlepage}
        \newgeometry{top=2.54cm,bottom=2.54cm,left=3.18cm,right=3.18cm}
        \thispagestyle{empty}
        % --- Logo和主标题 ---
        \begin{center}
            \includegraphics[width=10cm]{logo2.png} 
            
            \vspace{-0.6cm}
            
            {\hei \zihao{0} 学\hspace{0.5em}士\hspace{0.5em}学\hspace{0.5em}位\hspace{0.5em}论\hspace{0.5em}文\par}
        \end{center}
        
        \vfill 
        
        % --- 论文信息块和日期 ---
        \begin{center}
            % 使用一个单列、左对齐的表格来包裹所有信息，以实现左对齐
            \renewcommand{\arraystretch}{1.5}
            \begin{tabular}{l}
                % 论文题目 (第1行)
                {\hei \zihao{-3} 论文题目：\underline{\makebox[10cm][l]{\@title}}} \\
                
                % 论文题目 (第2行)
                {\hei \zihao{-3} \phantom{论文题目：}\underline{\makebox[10cm][l]{\hei \zihao{-3} \@titlesecondline}}} \\[1.5cm] 
                
                % 嵌套的姓名、学号表格
                % 这个表格本身是左对齐的，会和上面的 "论文题目" 左侧对齐
                \begin{tabular}{lc}
                    \hei \zihao{4} 姓\qquad 名：& \underline{\makebox[7cm][c]{\zihao{4} \@author}} \\
                    \hei \zihao{4} 学\qquad 号：& \underline{\makebox[7cm][c]{\zihao{4} \@stunum}} \\
                    \hei \zihao{4} 院\qquad 系：& \underline{\makebox[7cm][c]{\zihao{4} \@college}} \\
                    \hei \zihao{4} 专\qquad 业：& \underline{\makebox[7cm][c]{\zihao{4} \@major}} \\
                    \hei \zihao{4} 指导教师：& \underline{\makebox[7cm][c]{\zihao{4} \@instructor}} \\[0.5cm] 
                    
                    % 日期: 右对齐，但在右侧增加2个字符的空白(\hspace{2em})，从而让文字整体偏左
                    & \makebox[7cm][r]{\hei \zihao{4} \@chineseyear{}年\@chinesemonth{}月\hspace{2em}} \\
                \end{tabular}
            \end{tabular}
        \end{center}
        
        \vfill 
        
        \restoregeometry 
    \end{titlepage}
% --- 封面 2: 中文内封 ---
    \clearpage
    \begin{titlepage}
        \newgeometry{top=2.54cm,bottom=2.54cm,left=3.18cm,right=3.18cm}
        \thispagestyle{empty}
        \begin{center}
            \vfill 

            % --- 主标题: 宋体、一号 (26pt)、加粗 ---
            {\song \zihao{1} \bfseries 北京语言大学学士学位论文 \par}
            
            % --- 年级: 宋体、三号 (16pt)、不加粗 ---
            {\song \zihao{3} (\@grade) \par}
            
            \vspace{1.5cm} % 标题和表格之间的固定间距
            
            % --- 表格 1: 仅包含论文题目 ---
            \renewcommand{\arraystretch}{1.8}
            \begin{tabular}{ll}
                % --- “论文题目” 题头: 宋体小三 ---
                \song \zihao{-3} 论\ 文\ 题\ 目：& \underline{\makebox[10cm][l]{\hei \zihao{-3} \@title}} \\
                
                % (内容: 黑体小三)
                & \underline{\makebox[9cm][l]{\hei \zihao{-3} \@titlesecondline}} \\
            \end{tabular}

            % --- 在“论文题目”和“院系”之间插入“一段垂直间距” ---
            \vspace{5cm}

            % --- 表格 2: 包含其他信息 ---
            \renewcommand{\arraystretch}{1.8}
            % (使用 l@{}l 保证冒号紧跟表头)
            \begin{tabular}{l@{}l}
                % ---  使用 [s] (spread) 实现表头两端对齐 ---
                % ---  统一表头宽度为 9em (确保能容纳最长的表头) ---
                \makebox[9em][s]{\hei \zihao{-3} 院 系}： & \underline{\makebox[9cm][c]{\hei \zihao{-3} \@college}} \\
                \makebox[9em][s]{\hei \zihao{-3} 专 业}： & \underline{\makebox[9cm][c]{\hei \zihao{-3} \@major}} \\
                \makebox[9em][s]{\hei \zihao{-3} 学生姓名}： & \underline{\makebox[9cm][c]{\hei \zihao{-3} \@author}} \\
                \makebox[9em][s]{\hei \zihao{-3} 指导教师姓名}： & \underline{\makebox[9cm][c]{\hei \zihao{-3} \@instructor}} \\
                
                % --- 内容为 [c] (居中对齐) ---
                \makebox[9em][s]{\hei \zihao{-3} 论文完成日期}： & \underline{\makebox[9cm][c]{\hei \zihao{-3} \@arabicyear 年 6 月}}
            \end{tabular}
            
            \vfill 
            \vspace{2cm}
        \end{center}
        \restoregeometry 
    \end{titlepage}
    
    % --- 封面 3: 英文内封 ---
    \clearpage
    \begin{titlepage}
        \thispagestyle{empty}
        \begin{center}
            \vspace*{1cm}
            
            % 英文标题
            {\bfseries \zihao{3} \@entitle \par}
            \vspace{3cm}
            
            {\zihao{4} by \par}
            \vspace{0.5cm}
            {\bfseries \zihao{4} \@enauthor \par}
            \vspace{2cm}
            
            {\zihao{4} Supervised \par}
            {\zihao{4} by \par}
            \vspace{0.5cm}
            {\bfseries \zihao{4} \@eninstructor \par}
            
            \vfill
            
            {\zihao{4} Submitted to \@encollege \par}
            {\zihao{4} in partial fulfillment of the requirements for the degree of \par}
            {\zihao{4} \@degreetitle \par}
            \vspace{1cm}
            {\zihao{4} at \par}
            {\zihao{4} Beijing Language and Culture University \par}
            \vspace{2cm}
            {\zihao{4} June \@arabicyear \par}
        \end{center}
    \end{titlepage}
    
    % --- 封面 4: 声明页---
    \clearpage
    \thispagestyle{empty}
    % 声明页头部：校徽 + 校名
    \begin{center}
        \includegraphics[width=9cm]{logo1.png}
    \end{center}
    \vspace{0.8cm}
    % 声明标题 - 黑体三号
    \begin{center}
        {\hei \zihao{3} 论文原创性声明}
    \end{center} 
    \vspace{0.3cm}
    % 声明内容 - 宋体小四号，首行缩进2字符
    {\song \zihao{-4} \setlength{\baselineskip}{20pt}
    \par\indent 本人郑重声明：所呈交的论文，是本人在导师指导下，独立进行的研究工作及取得的研究成果。尽我所知，除了文中已经注明引用和致谢的地方外，论文中不包含其他人或集体已经发表或撰写的研究成果，也不包含为获得北京语言大学或其他教育机构的学位或证书所使用过的材料。对本文的研究做出重要贡献的个人和集体，均已在文中以明确方式标明。本人完全意识到本声明的法律结果由本人承担。
    }
    \vspace{1cm}
    % 签名区域 - 宋体小四号
    \begin{flushright}
        \song \zihao{-4}
        签\qquad 名：\underline{\makebox[5em]{}} \\[0.8em]
        日\qquad 期：\underline{\makebox[5em]{}}
    \end{flushright}
    \vspace{0.8cm}
    % 学位论文知识产权授属声明
    \begin{center}
        {\hei \zihao{3} 学位论文知识产权授属声明}
    \end{center}
    \vspace{0.3cm}
    {\song \zihao{-4} \setlength{\baselineskip}{20pt}
    \par\indent 本人郑重声明：本人所呈交论文，是在导师指导下完成的，论文知识产权归属北京语言大学。学校有权保留并向国家有关部门或机构送交论文的复印件和电子版本，允许论文被查询和借阅，将论文编入有关数据库进行检索等。本人离校后发表或使用学位论文或与该论文直接相关的学术论文获取的果实，署名仍为北京语言大学。
    }
    \vspace{1cm}
    \begin{flushright}
        \song \zihao{-4}
        签\qquad 名：\underline{\makebox[5em]{}} \\[0.8em]
        导师签名：\underline{\makebox[5em]{}} \\[0.8em]
        日\qquad 期：\underline{\makebox[5em]{}}
    \end{flushright}

}
\makeatother

% ====================================================
% 7. 页眉页脚设置
% ====================================================

% --- 定义一个布尔开关，用于区分“前言”和“正文” ---
\newif\ifmainbody
\mainbodyfalse % 文档开始时，默认为“前言”部分

% --- 样式1：正文页 (mainstyle) ---
% 用于正文、参考文献、附录、致谢
\fancypagestyle{mainstyle}{
    \fancyhf{}
    \fancyhead[C]{\song \zihao{5} 北京语言大学信息科学学院学士学位论文}
    \fancyfoot[C]{\zihao{5} {\song 第}~\thepage~{\song 页 \quad 共}~\pageref{LastPage}~{\song 页}}
    \renewcommand{\headrulewidth}{0.75pt}
    \renewcommand{\footrulewidth}{0pt}
}

% --- 样式2：前言页 (romanstyle) ---
% 用于目录、中英文摘要
\fancypagestyle{romanstyle}{
    \fancyhf{}
    \fancyfoot[C]{\zihao{5} \Roman{page}}
    \renewcommand{\headrulewidth}{0pt}
    \renewcommand{\footrulewidth}{0pt}
}

% --- 核心逻辑：定义一个智能的 plain 页面样式 ---
% LaTeX 在每个 \chapter 首页默认使用 plain 样式。
% 让 plain 根据 \ifmainbody 开关自动选择正确的页眉页脚。
\fancypagestyle{plain}{
    \fancyhf{}
    \ifmainbody
        % 如果是正文部分，章节首页的样式应与 mainstyle 相同
        \fancyhead[C]{\song \zihao{5} 北京语言大学信息科学学院学士学位论文}
        \fancyfoot[C]{\zihao{5} {\song 第}~\thepage~{\song 页 \quad 共}~\pageref{LastPage}~{\song 页}}
        \renewcommand{\headrulewidth}{0.75pt}
        \renewcommand{\footrulewidth}{0pt}
    \else
        % 如果是前言部分 (如目录)，章节首页应与 romanstyle 相同 (仅有罗马页脚)
        \fancyfoot[C]{\zihao{5} \Roman{page}}
        \renewcommand{\headrulewidth}{0pt}
        \renewcommand{\footrulewidth}{0pt}
    \fi
}

% ====================================================
% 8. 章节格式设置
% ====================================================
\ctexset{
    chapter = {
        break = {}, 
        number = \chinese{chapter},
        % 格式：小二号(-2)、黑体(\hei)、加粗(\bfseries)、居中(\centering)、2倍行距(\linespread{1.6})
        format = \centering\hei\zihao{-2}\bfseries\linespread{1.6}\selectfont,
        name = {第,章},
        nameformat = {},
        titleformat = {},
        % “留出一个汉字的空间”：使用 \ccwd (当前字体的汉字宽度)
        aftername = \hspace{\ccwd}, 
        beforeskip = 17pt,   % 段前 17 磅
        afterskip = 17pt,    % 段后 17 磅
        fixskip = true,
    },
    section = {
        % 格式：小三号(-3)、黑体(\hei)、加粗(\bfseries)、左对齐、1.5倍行距(\linespread{1.3})
        format = \raggedright\hei\zihao{-3}\bfseries\linespread{1.3}\selectfont,
        beforeskip = 13pt,   % 段前 13 磅
        afterskip = 13pt,    % 段后 13 磅
    },
    subsection = {
        % 格式：四号(4)、黑体(\hei)、【不加粗】、左对齐、单倍行距(\linespread{1.0})
        format = \raggedright\hei\zihao{4}\linespread{1.0}\selectfont, 
        beforeskip = 13pt,   % 段前 13 磅
        afterskip = 13pt,    % 段后 13 磅
    }
}

% ====================================================
% 9. 正文格式设置
% ====================================================
\renewcommand{\normalsize}{%
    \fontsize{12pt}{20pt}\selectfont
    \abovedisplayskip 12pt plus 3pt minus 9pt
    \belowdisplayskip \abovedisplayskip
    \abovedisplayshortskip 0pt plus 3pt
    \belowdisplayshortskip 6.5pt plus 3.5pt minus 3pt
}
\setlength{\parindent}{2em} % 首行缩进2字符

% ====================================================
% 10. 摘要与关键词 
% ====================================================
\makeatletter
\newcommand{\@mycnabstract}{
    \clearpage
    \mainbodyfalse      % 标记：进入前言部分
    \pagenumbering{Roman} % 页码：为摘要开启独立的罗马数字
    \pagestyle{romanstyle} % 页面样式：应用 romanstyle
    \phantomsection
    \addcontentsline{toc}{chapter}{摘要}
    \vspace*{0.5\baselineskip} % 在页首插入固定垂直空白

    % --- 论文题目 (三号黑体加粗, 居中, 1.5倍行距) ---
    \begin{center}
        \setstretch{1.5}
        {\hei \zihao{-3} \bfseries \@title\par}
    \end{center}
    \vspace{0.2\baselineskip} % 标题下的间距

    % --- 作者与指导教师 (Times New Roman五号加粗, 居中, 1.5倍行距) ---
    {\centering
     \setstretch{1.5}
     \rmfamily \zihao{5} \bfseries
     作者: \@author \hspace{6em} 导师: \@instructor \par}
    \vspace{0.5\baselineskip} % 作者行与摘要正文的间距

    % --- 摘要标签和正文 (补充了 \rmfamily 确保中英文混排严格合规) ---
    \noindent {\hei \zihao{-4} 摘要：}\rmfamily\kaishu \zihao{5}\setlength{\baselineskip}{20pt}
}

\newcommand{\@myenabstract}{
    \clearpage
    \mainbodyfalse % 标记此处不是正文
    \pagestyle{romanstyle} % 使用罗马页码样式
    \phantomsection
    \addcontentsline{toc}{chapter}{Abstract}
    \vspace*{0.5\baselineskip}
    
    % --- 英文标题 (三号TNR加粗, 居中, 1.5倍行距) ---
    \begin{center}
    \setstretch{1.5}
    {\rmfamily \zihao{-3} \bfseries \@entitle\par}
    \end{center}
    \vspace{0.2\baselineskip} % 减小标题与作者行的间距

    % --- Author与Tutor (修正为 \centering，让1.5倍行距生效) ---
    {\centering
    \setstretch{1.5}
    \rmfamily \zihao{5} \bfseries
    Author: \@enauthor \hspace{6em} Tutor: \@eninstructor \par}
    
    \vspace{0.5\baselineskip} % 作者行与摘要正文的间距

    % --- Abstract标签和正文 (补充了 \rmfamily 确保全部为TNR字体) ---
    \noindent {\rmfamily \zihao{5} \bfseries Abstract: }\rmfamily\zihao{5}\setlength{\baselineskip}{20pt}
}

\newenvironment{cnabstract}{
    \@mycnabstract
}{\par} % \par 保证结束环境时，按照 20pt 结算最后一段的行距

\newcommand{\cnkeywords}[1]{
    \par % 结束摘要正文段落
    \vspace{\baselineskip} % 在摘要和关键词之间空一行
    % “关键词”小四号黑体，正文五号楷体 (加 \rmfamily 保护其中的英文字母)，固定20磅行距
    \noindent{\hei \zihao{-4} 关键词：}{\rmfamily\kaishu \zihao{5}\setlength{\baselineskip}{20pt} #1\par}
}

\newenvironment{enabstract}{
    \@myenabstract
}{
    \par % 结束摘要段落，结算行距
    \clearpage % 强制分页
    % --- 从这里开始，正式进入正文部分 ---
    \mainbodytrue          % 1. 激活正文开关
    \pagenumbering{arabic}  % 2. 页码切换为阿拉伯数字，并从1开始
    \pagestyle{mainstyle}   % 3. 启用正文页面样式 
}

\newcommand{\enkeywords}[1]{
    \par % 结束摘要正文段落
    \vspace{\baselineskip} % 在摘要和关键词之间空一行
    \noindent % 关键词段落不进行首行缩进
    {\rmfamily \zihao{5} \bfseries Key Words: } % 标签也声明为 TNR
    {\rmfamily \zihao{5}\setlength{\baselineskip}{20pt} #1\par} % 英文关键词也应用 TNR 及 20磅行距
}

\makeatother
% ====================================================
% 11. 目录格式设置
% ====================================================
\renewcommand{\contentsname}{\centerline{\hei \zihao{3} 目\quad\quad\quad 录}}

% 字体 (五号黑体)
\renewcommand{\cftchapfont}{\hei \zihao{5}}
\renewcommand{\cftchappagefont}{\hei \zihao{5}}
\renewcommand{\cftsecfont}{\hei \zihao{5}}
\renewcommand{\cftsecpagefont}{\hei \zihao{5}}
\renewcommand{\cftsubsecfont}{\hei \zihao{5}}
\renewcommand{\cftsubsecpagefont}{\hei \zihao{5}}

% 目录项前导点
\renewcommand{\cftchapleader}{\cftdotfill{\cftdotsep}} 
\renewcommand{\cftsecleader}{\cftdotfill{\cftdotsep}}
\renewcommand{\cftsubsecleader}{\cftdotfill{\cftdotsep}}

% 目录缩进
\setlength{\cftsecindent}{1em}      % 二级标题缩进
\setlength{\cftsubsecindent}{2em}   % 三级标题缩进

% 目录行间距 (条目之间的额外间距设为0，由基础行距接管)
\setlength{\cftbeforechapskip}{0pt}
\setlength{\cftbeforesecskip}{0pt}
\setlength{\cftbeforesubsecskip}{0pt}

\let\oldtableofcontents\tableofcontents
\renewcommand{\tableofcontents}{
  \clearpage
  \mainbodyfalse         % 标记：进入前言部分
  \pagenumbering{Roman}  % 页码：为目录设置独立的罗马数字 (从 I 开始)
  \pagestyle{romanstyle} % 页面样式：应用 romanstyle
  
  % 推荐做法：使用 spacing 环境包裹目录内容，确保行距严格生效并自动处理段落与范围
  \begin{spacing}{1.5}
    \oldtableofcontents
  \end{spacing}
}
% ====================================================
% 12. 图表与公式格式设置
% ====================================================
\RequirePackage{caption} 

% --- 1. 定义标签分隔符：严格留出一个汉字宽度 ---
\DeclareCaptionLabelSeparator{chinesequad}{\hspace{\ccwd}}

% --- 2. 定义标题字体：五号宋体 ---
\DeclareCaptionFont{songfive}{\song\zihao{5}}

% --- 3. caption 全局设置 ---
\captionsetup{
    labelsep=chinesequad, % 标签后留一个汉字空间
    figurename={图},
    tablename={表},
    font=songfive,        % 标签和标题内容均为五号宋体
    labelfont=songfive,   % 标签(如"图1.1")不加粗，使用宋体
    textfont=songfive     % 标题内容使用宋体
}

% --- 4. 表格标题特有设置 ---
% 要求：居表上方正中，标题与表格无间隔
\captionsetup[table]{
    position=top,
    skip=0pt             % 设置为0pt，实现"无间隔"
}

% --- 5. 图片标题特有设置 ---
% 要求：居图下方正中，标题与图表隔一行
\captionsetup[figure]{
    position=bottom,
    skip=1.5\baselineskip % 设置约一行的高度间距
}

% --- 6. 公式编号设置 ---
% 要求：编号格式为四号 Times New Roman，右对齐
% (LaTeX 默认右对齐，此处主要控制字体大小和族)
\makeatletter
\renewcommand{\maketag@@@}[1]{\hbox{\m@th\normalsize\zihao{4}#1}}
\makeatother

% ====================================================
% 13. 参考文献格式设置
% ====================================================
\makeatletter
\renewcommand{\bibsection}{
    \clearpage
    \mainbodytrue           % 确保页眉页脚正确
    \pagestyle{mainstyle} 
    \phantomsection
    \addcontentsline{toc}{chapter}{参考文献}
    
    % 【标题格式】
    % ：五号(\zihao{5})、黑体(\hei)、加粗(\bfseries)、左对齐(\raggedright)
    \begingroup
        \raggedright
        \hei\zihao{5}\bfseries 参考文献
        \par
    \endgroup
    
    % 标题后间距 
    \vspace{13pt} 
}
\makeatother

% 【列表间距】条目之间不留额外空隙
\setlength{\bibsep}{0pt}

\renewcommand{\bibfont}{\zihao{5}\linespread{1.0}\selectfont}

% ====================================================
% 14. 附录格式设置 
% ====================================================
% 请确保导言区加载了 \usepackage{etoolbox} 以便使用 \appto 命令

\appto\appendix{
    \mainbodytrue % 确保附录使用正文页眉页脚样式
    
    % ==========================================
    % 1. 附录标题格式设置 
    % ==========================================
    \ctexset{
        chapter = {
            % 要求“附录页不接排，需另起一页”，因此必须强制换页
            break = \clearpage, 
            number = \Alph{chapter},
            name = {附录,},
            % 格式：左对齐(\raggedright)、小二(\zihao{-2})、黑体(\hei)、加粗(\bfseries)、2倍行距
            format = \raggedright\hei\zihao{-2}\bfseries\linespread{1.6}\selectfont,
            % 要求：“留出一个汉字的空间”
            aftername = \hspace{\ccwd},
            beforeskip = 17pt,  % 段前 17 磅
            afterskip = 17pt,   % 段后 17 磅
            fixskip = true,
        }
    }

    % ==========================================
    % 2. 附录正文格式设置
    % ==========================================
    \song\zihao{-4} % 小四号宋体 
    \setlength{\baselineskip}{20pt} % 行距固定值：20 磅（必须紧跟在字号命令后面！）
    \setlength{\parindent}{2em} % 首行缩进 2 字符
}
% ====================================================
% 15. 致谢 
% ====================================================
\newenvironment{acknowledgements}{
    \clearpage
    \mainbodytrue 
    \pagestyle{mainstyle} % 确保致谢页使用正文页脚
    \phantomsection
    \addcontentsline{toc}{chapter}{致谢}
    {\raggedright \hei \zihao{-4} \bfseries 致\quad 谢\par}
    \vspace{13pt}
    \markboth{致谢}{致谢}
    \setstretch{1.5}
    \song \zihao{-4}
    \setlength{\parindent}{2em}
    \par
}{\par}

% ====================================================
% 16. 其他设置
% ====================================================
\hypersetup{
    colorlinks=false,
    pdfborder={0 0 0},
}
\renewcommand{\thefootnote}{\arabic{footnote}}

% 文档初始页面样式设为空白，用于封面
\pagestyle{empty}